"""Build the schema description the model sees, and ground its literals in real values.

The single biggest accuracy problem in text-to-SQL over business data isn't the
SQL - it's the values. This data contains 'EPC PROECTS' (a typo, in every row),
'SUBCONTRACT, E&C CHARGES', 'BOI-169'. A model asked about "EPC projects" will
confidently write PROJECT TYPE = 'EPC PROJECTS' and get zero rows back. So we
paste every low-cardinality value list into the prompt, and when a query returns
nothing we fuzzy-match the literals and retry once.
"""
from __future__ import annotations

import difflib
import re
from functools import lru_cache
from pathlib import Path

from .config import DB_PATH, TABLE_NAME
from .database import ENUM_COLUMNS, FUZZY_COLUMNS, connect

COLUMN_NOTES = {
    "txn_date": "TEXT, ISO 'YYYY-MM-DD'. Compare as a string: txn_date >= '2025-04-01'.",
    "party_name": "TEXT. Vendor / customer / employee name, UPPERCASE, ~3200 distinct. Match with LIKE '%FRAGMENT%'.",
    "bank": "TEXT. Account the money moved through. 'CC-' prefix means the credit/cash-credit facility on that account.",
    "project": "TEXT. Project name as entered, ~148 distinct. Match with LIKE.",
    "project_std": "TEXT. Standardised project name. Prefer this for grouping by project.",
    "exp_type": "TEXT. Expense or receipt category, 69 distinct. See values below.",
    "flow_type": "TEXT. 'EXPENSE' (money out) or 'RECEIPT' (money in).",
    "project_type": "TEXT. Business line. Note the source spells it 'EPC PROECTS' / 'AQL PROECTS' - keep the typo.",
    "amount": "REAL. Always positive - the direction is in flow_type, not the sign.",
    "month_label": "TEXT, e.g. 'Apr 2025'. Display only - it does NOT sort correctly.",
    "month_key": "TEXT, e.g. '2025-04'. Use this to group or sort by month.",
    "fiscal_year": "TEXT, e.g. '2025-26'. Indian fiscal year, April to March.",
    "quarter": "TEXT, e.g. '2025Q2'. Calendar quarter.",
    "partner": "TEXT. Group entity that owns the transaction.",
    "team": "TEXT. Internal business unit.",
    "signed_amount": "REAL. +amount for RECEIPT, -amount for EXPENSE. SUM(signed_amount) is net cash flow.",
}

SEMANTIC_RULES = """\
Business rules you must follow:
- "spend", "expense", "paid", "outflow", "payment" -> WHERE flow_type = 'EXPENSE'
- "received", "collection", "receipt", "inflow", "income" -> WHERE flow_type = 'RECEIPT'
- "net cash flow", "net position", "surplus" -> SUM(signed_amount)
- Never SUM(amount) across both flow types; that adds inflow to outflow.
- Money is in Indian Rupees. Never invent a currency conversion.
- "top N" / "biggest" -> GROUP BY the entity, ORDER BY the aggregate DESC, LIMIT N.
- "this year" with no other qualifier means the latest fiscal_year in the data.
- For a named party or project, use LIKE '%FRAGMENT%' with an uppercase fragment.
- Always alias aggregates with a readable name, e.g. SUM(amount) AS total_expense.
"""

FEW_SHOTS = """\
Q: What did we spend in total last month?
SQL: SELECT month_key, SUM(amount) AS total_expense
     FROM cash_flow
     WHERE flow_type = 'EXPENSE'
       AND month_key = (SELECT MAX(month_key) FROM cash_flow)
     GROUP BY month_key;

Q: Top 5 vendors by payment in FY 2025-26
SQL: SELECT party_name, SUM(amount) AS total_paid, COUNT(*) AS txn_count
     FROM cash_flow
     WHERE flow_type = 'EXPENSE' AND fiscal_year = '2025-26'
     GROUP BY party_name
     ORDER BY total_paid DESC
     LIMIT 5;

Q: Monthly net cash flow for the PAANI team
SQL: SELECT month_key, SUM(signed_amount) AS net_cash_flow
     FROM cash_flow
     WHERE team = 'PAANI'
     GROUP BY month_key
     ORDER BY month_key;

Q: How much has the Dungarpur project cost us, broken down by expense type?
SQL: SELECT exp_type, SUM(amount) AS total_expense
     FROM cash_flow
     WHERE flow_type = 'EXPENSE' AND project_std LIKE '%DUNGARPUR%'
     GROUP BY exp_type
     ORDER BY total_expense DESC;
"""


@lru_cache(maxsize=4)
def _enum_values(db_path_str: str) -> dict[str, tuple[str, ...]]:
    conn = connect(Path(db_path_str))
    try:
        out = {}
        for col in ENUM_COLUMNS:
            rows = conn.execute(
                f"SELECT DISTINCT {col} FROM {TABLE_NAME} "
                f"WHERE {col} IS NOT NULL ORDER BY {col}"
            ).fetchall()
            out[col] = tuple(r[0] for r in rows)
        return out
    finally:
        conn.close()


def enum_values(db_path: Path = DB_PATH) -> dict[str, tuple[str, ...]]:
    return _enum_values(str(db_path))


def schema_card(db_path: Path = DB_PATH) -> str:
    """The schema block injected into every prompt."""
    conn = connect(db_path)
    try:
        cols = conn.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()
        n_rows = conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0]
        lo, hi = conn.execute(
            f"SELECT MIN(txn_date), MAX(txn_date) FROM {TABLE_NAME}"
        ).fetchone()
        fys = [r[0] for r in conn.execute(
            f"SELECT DISTINCT fiscal_year FROM {TABLE_NAME} ORDER BY fiscal_year"
        )]
    finally:
        conn.close()

    lines = [
        f"Table: {TABLE_NAME}  ({n_rows:,} rows, one row per bank transaction)",
        f"Date coverage: {lo} to {hi}. Fiscal years present: {', '.join(fys)}.",
        "",
        "Columns:",
    ]
    for c in cols:
        name, ctype = c["name"], c["type"]
        note = COLUMN_NOTES.get(name, "")
        lines.append(f"  {name} ({ctype}) - {note}")

    lines += ["", "Exact values for the categorical columns (use these verbatim):"]
    vals = enum_values(db_path)
    for col, values in vals.items():
        joined = ", ".join(f"'{v}'" for v in values)
        lines.append(f"  {col}: {joined}")

    lines += [
        "",
        f"High-cardinality columns - match with LIKE, never '=': {', '.join(FUZZY_COLUMNS)}",
    ]
    return "\n".join(lines)


# ------------------------------------------------------- value grounding


_LITERAL = re.compile(r"'([^']*)'")


def suggest_value_fixes(sql: str, db_path: Path = DB_PATH) -> list[str]:
    """Find string literals in the SQL that don't exist in any enum column.

    Returns human-readable hints, e.g.
      "'EPC PROJECTS' is not a value in project_type. Closest match: 'EPC PROECTS'."
    Fed back to the model on a zero-row retry instead of being silently rewritten,
    so the correction stays visible and auditable.
    """
    vals = enum_values(db_path)
    everything = {v.upper(): (col, v) for col, values in vals.items() for v in values}
    hints: list[str] = []

    for literal in set(_LITERAL.findall(sql)):
        if not literal or literal.upper() in everything:
            continue
        if re.fullmatch(r"[\d\-%_ .]*", literal):      # dates, LIKE wildcards, numbers
            continue
        pool = list(everything.keys())
        close = difflib.get_close_matches(literal.upper(), pool, n=1, cutoff=0.55)
        if close:
            col, real = everything[close[0]]
            hints.append(
                f"'{literal}' does not exist in the data. "
                f"The closest real value is '{real}' in column {col}."
            )
    return hints


def sample_values(column: str, fragment: str, limit: int = 8,
                  db_path: Path = DB_PATH) -> list[str]:
    """Look up real party/project names containing a fragment. Used to help the
    model when a name-based filter finds nothing."""
    if column not in FUZZY_COLUMNS + ENUM_COLUMNS:
        raise ValueError(f"Not a searchable column: {column}")
    conn = connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT {column}, COUNT(*) AS n FROM {TABLE_NAME} "
            f"WHERE {column} LIKE ? GROUP BY {column} ORDER BY n DESC LIMIT ?",
            (f"%{fragment.upper()}%", limit),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()
