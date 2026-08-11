"""Turn the Details sheet into a queryable SQLite table, and run generated SQL safely.

Why SQLite rather than handing the LLM a DataFrame: SQL is the thing these models
are actually trained on, the query is auditable before it runs, and a read-only
connection with no exec() means a bad generation can't do anything worse than
return the wrong rows.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from pathlib import Path

import pandas as pd

from .config import DB_PATH, MAX_ROWS_RETURNED, SHEET_NAME, SQL_TIMEOUT_SECONDS, TABLE_NAME
from .fsutil import atomic_replace, sqlite_ro_uri, temp_sibling

log = logging.getLogger(__name__)

# Sheet header -> SQL column. Renamed because "Exp. Type" and "EXP REC" force
# quoting in every query, and small models drop the quotes and break the SQL.
COLUMN_MAP = {
    "Date": "txn_date",
    "Party Name": "party_name",
    "Bank": "bank",
    "Name of Project": "project",
    "Name of Project as per format": "project_std",
    "Exp. Type": "exp_type",
    "EXP REC": "flow_type",
    "PROJECT TYPE": "project_type",
    "Amount": "amount",
    "Month": "month_label",
    "Partner": "partner",
    "Team": "team",
}

# Columns whose full value list is small enough to paste into the prompt.
ENUM_COLUMNS = ["flow_type", "project_type", "partner", "team", "bank", "exp_type"]

# High-cardinality text the model should match with LIKE, never equality.
FUZZY_COLUMNS = ["party_name", "project", "project_std"]


def _clean_text(value):
    if not isinstance(value, str):
        return value
    # NBSP (\xa0) is all over the Exp. Type column; it makes equality filters
    # silently return zero rows.
    value = unicodedata.normalize("NFKC", value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _fiscal_year(ts: pd.Timestamp) -> str | None:
    """Indian FY: April to March. 2025-06-01 -> '2025-26'."""
    if pd.isna(ts):
        return None
    start = ts.year if ts.month >= 4 else ts.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def load_dataframe(xlsx_path: Path) -> pd.DataFrame:
    df = pd.read_excel(xlsx_path, sheet_name=SHEET_NAME)
    df = df.rename(columns=COLUMN_MAP)
    df = df[[c for c in COLUMN_MAP.values() if c in df.columns]]

    for col in df.select_dtypes(include=["object", "string"]).columns:
        df[col] = df[col].map(_clean_text)

    df["txn_date"] = pd.to_datetime(df["txn_date"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df = df.dropna(subset=["txn_date", "amount"])

    df["flow_type"] = df["flow_type"].str.upper()

    # Derived columns that save the model from writing fragile date arithmetic.
    df["month_key"] = df["txn_date"].dt.strftime("%Y-%m")     # sortable: '2025-04'
    df["fiscal_year"] = df["txn_date"].map(_fiscal_year)      # '2025-26'
    df["quarter"] = df["txn_date"].dt.to_period("Q").astype(str)

    # Amounts are stored positive; direction lives in flow_type. Pre-signing it
    # means "net cash flow" is SUM(signed_amount) instead of a CASE expression
    # the model has to remember to write.
    df["signed_amount"] = df["amount"].where(df["flow_type"] == "RECEIPT", -df["amount"])

    df["txn_date"] = df["txn_date"].dt.strftime("%Y-%m-%d")
    return df.reset_index(drop=True)


def build_database(xlsx_path: Path, db_path: Path = DB_PATH) -> dict:
    df = load_dataframe(xlsx_path)

    # Unique temp name so a locked leftover from a failed run can't block us.
    tmp = temp_sibling(db_path, "building")
    tmp.unlink(missing_ok=True)

    # NOTE: `with sqlite3.connect(...)` manages the TRANSACTION, not the connection -
    # it commits but leaves the handle open, which makes the rename below fail on
    # Windows. The connection has to be closed explicitly.
    conn = sqlite3.connect(tmp)
    try:
        df.to_sql(TABLE_NAME, conn, index=False, if_exists="replace")
        for col in ["txn_date", "month_key", "fiscal_year", "flow_type",
                    "project_type", "team", "partner", "bank", "exp_type", "project"]:
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{col} ON {TABLE_NAME}({col})")
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()

    try:
        atomic_replace(tmp, db_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    log.info("Built %s with %d rows", db_path, len(df))
    return {
        "rows": len(df),
        "date_min": df["txn_date"].min(),
        "date_max": df["txn_date"].max(),
    }


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Read-only connection. A generated query physically cannot write."""
    conn = sqlite3.connect(sqlite_ro_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------------ guard rails

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|"
    r"PRAGMA|VACUUM|GRANT|REINDEX)\b",
    re.IGNORECASE,
)


class UnsafeQuery(ValueError):
    pass


def sanitize_sql(sql: str) -> str:
    """Allow exactly one read-only statement, and cap the rows it can return."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise UnsafeQuery("Empty query.")

    # Reject stacked statements. A lone trailing ';' was already stripped above.
    stripped = re.sub(r"'[^']*'", "''", sql)          # ignore semicolons in literals
    if ";" in stripped:
        raise UnsafeQuery("Only one statement is allowed.")

    if not re.match(r"^(SELECT|WITH)\b", sql, re.IGNORECASE):
        raise UnsafeQuery("Only SELECT / WITH queries are allowed.")
    if _FORBIDDEN.search(stripped):
        raise UnsafeQuery("Query contains a write or schema-changing keyword.")

    if not re.search(r"\bLIMIT\s+\d+", sql, re.IGNORECASE):
        sql = f"{sql}\nLIMIT {MAX_ROWS_RETURNED}"
    return sql


def run_query(sql: str, db_path: Path = DB_PATH) -> pd.DataFrame:
    safe = sanitize_sql(sql)
    conn = connect(db_path)
    try:
        # Interrupt a runaway query rather than hanging the chat turn.
        deadline = SQL_TIMEOUT_SECONDS * 1000
        ticks = {"n": 0}

        def watchdog():
            ticks["n"] += 1
            return 1 if ticks["n"] * 10 > deadline else 0

        conn.set_progress_handler(watchdog, 10_000)
        return pd.read_sql_query(safe, conn)
    finally:
        conn.close()
