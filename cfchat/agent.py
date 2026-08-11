"""The question-answering loop.

  question
    -> plan (SQL, or a clarifying question if genuinely ambiguous)
    -> sanitize
    -> execute
    -> on error or zero rows: repair once or twice with the error / value hints
    -> summarise the result set in prose

Every turn returns the SQL it ran, so the numbers are checkable. That matters
more than it sounds for finance data: a confident wrong total is worse than
no answer, and the query is the audit trail.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .config import DB_PATH, MAX_REPAIR_ATTEMPTS, MAX_ROWS_TO_MODEL
from .database import UnsafeQuery, run_query, sanitize_sql
from .llm import Backend, LLMError, LLMUnavailable, get_backend, parse_json
from .schema import FEW_SHOTS, SEMANTIC_RULES, schema_card, suggest_value_fixes

log = logging.getLogger(__name__)

PLANNER_SYSTEM = """You are a SQL analyst for a construction and water-infrastructure \
company's cash-flow ledger. You translate business questions into SQLite queries.

{schema}

{rules}

Worked examples:
{shots}

Respond with JSON only, no prose outside it, in one of these two shapes:

  {{"action": "sql", "sql": "SELECT ...", "reasoning": "one short sentence"}}

  {{"action": "clarify", "question": "the one thing you need to know"}}

Use "clarify" only when the question cannot be answered any sensible way - not \
merely because more detail would be nice. If a reasonable default exists, take it \
and say so in "reasoning". SQLite dialect only: no window function you haven't \
verified, no DATE_TRUNC, no EXTRACT. Use the derived columns (month_key, \
fiscal_year, quarter, signed_amount) instead of computing dates yourself."""

ANSWER_SYSTEM = """You are reporting query results to a finance manager. Be direct \
and specific. State the numbers with thousands separators and the rupee symbol \
(e.g. ₹1,23,45,678 - Indian grouping). Two to four sentences unless the data \
genuinely needs more. Point out anything notable in the result: a dominant \
contributor, an outlier, an empty result. Never invent a number that is not in \
the rows you were given, and never speculate about causes the data doesn't show."""


def is_effectively_empty(df: pd.DataFrame) -> bool:
    """True when the query found nothing.

    `df.empty` is not enough: `SELECT SUM(amount) ... WHERE <no match>` returns
    one row containing NULL, which looks like a successful answer and would
    otherwise be reported to the user as a real total.
    """
    if df.empty:
        return True
    return len(df) == 1 and df.iloc[0].isna().all()


@dataclass
class Turn:
    question: str
    answer: str = ""
    sql: str = ""
    rows: pd.DataFrame | None = None
    reasoning: str = ""
    attempts: list[str] = field(default_factory=list)
    needs_clarification: bool = False
    error: str | None = None


class CashFlowAgent:
    def __init__(self, backend: Backend | None = None, db_path: Path = DB_PATH):
        self.backend = backend or get_backend()
        self.db_path = db_path
        self.history: list[Turn] = []

    # ---------------------------------------------------------------- prompts

    def _planner_system(self) -> str:
        return PLANNER_SYSTEM.format(
            schema=schema_card(self.db_path), rules=SEMANTIC_RULES, shots=FEW_SHOTS
        )

    def _context(self) -> str:
        """Last two turns, so follow-ups like 'and for the JAL team?' resolve."""
        if not self.history:
            return ""
        parts = []
        for turn in self.history[-2:]:
            if turn.sql:
                parts.append(f"Earlier question: {turn.question}\nSQL used: {turn.sql}")
        return ("Conversation so far (for resolving follow-up references):\n"
                + "\n\n".join(parts) + "\n\n") if parts else ""

    # ---------------------------------------------------------------- main entry

    def ask(self, question: str) -> Turn:
        turn = Turn(question=question)
        system = self._planner_system()
        user = f"{self._context()}Question: {question}"

        try:
            plan = parse_json(self.backend.complete(system, user, json_mode=True))
        except LLMUnavailable:
            raise
        except LLMError as exc:
            turn.error = str(exc)
            turn.answer = "I couldn't get a usable response from the model. Try rephrasing."
            self.history.append(turn)
            return turn

        if plan.get("action") == "clarify":
            turn.needs_clarification = True
            turn.answer = plan.get("question", "Could you be more specific?")
            self.history.append(turn)
            return turn

        sql = plan.get("sql", "")
        turn.reasoning = plan.get("reasoning", "")
        feedback: str | None = None

        for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
            if feedback:
                sql = self._repair(system, question, sql, feedback)
                if not sql:
                    break
            turn.attempts.append(sql)

            try:
                safe = sanitize_sql(sql)
                df = run_query(safe, self.db_path)
            except UnsafeQuery as exc:
                feedback = f"That query was rejected: {exc} Write a single read-only SELECT."
                continue
            except Exception as exc:                     # sqlite3.OperationalError etc.
                feedback = f"SQLite rejected the query with: {exc}. Fix it."
                continue

            if is_effectively_empty(df) and attempt < MAX_REPAIR_ATTEMPTS:
                hints = suggest_value_fixes(sql, self.db_path)
                if hints:
                    feedback = ("The query ran but returned no rows. Likely cause: "
                                + " ".join(hints)
                                + " Rewrite using the real values.")
                    continue
                feedback = ("The query ran but returned no rows. Check whether a filter "
                            "is too narrow, or whether a name filter should use "
                            "LIKE '%FRAGMENT%' instead of '='.")
                continue

            turn.sql, turn.rows = safe, df
            turn.answer = self._summarise(question, safe, df)
            self.history.append(turn)
            return turn

        turn.error = feedback
        turn.sql = sql
        turn.answer = (
            "I couldn't build a query that works for that question. "
            "Rephrasing it, or naming the project, team or period explicitly, usually fixes it."
        )
        self.history.append(turn)
        return turn

    # ---------------------------------------------------------------- helpers

    def _repair(self, system: str, question: str, bad_sql: str, feedback: str) -> str:
        prompt = (
            f"Question: {question}\n\n"
            f"Your previous SQL:\n{bad_sql}\n\n"
            f"{feedback}\n\n"
            'Reply with JSON only: {"action": "sql", "sql": "...", "reasoning": "..."}'
        )
        try:
            plan = parse_json(self.backend.complete(system, prompt, json_mode=True))
        except LLMError:
            return ""
        return plan.get("sql", "")

    def _summarise(self, question: str, sql: str, df: pd.DataFrame) -> str:
        if is_effectively_empty(df):
            return "No transactions match that. The filters may be too narrow, or the data may not cover that period."

        shown = df.head(MAX_ROWS_TO_MODEL)
        truncated = "" if len(df) <= MAX_ROWS_TO_MODEL else (
            f"\n(showing {MAX_ROWS_TO_MODEL} of {len(df)} rows)"
        )
        prompt = (
            f"Question: {question}\n\n"
            f"SQL executed:\n{sql}\n\n"
            f"Result ({len(df)} row(s)):\n{shown.to_markdown(index=False)}{truncated}\n\n"
            "Answer the question from these rows."
        )
        try:
            return self.backend.complete(ANSWER_SYSTEM, prompt)
        except LLMError:
            # Still useful without the prose layer.
            return f"Query returned {len(df)} row(s):\n\n{shown.to_markdown(index=False)}"


# ---------------------------------------------------------------- CLI


def main() -> None:
    import argparse

    from .pipeline import ensure_ready

    parser = argparse.ArgumentParser(description="Ask the cash-flow ledger a question.")
    parser.add_argument("question", nargs="*", help="Leave empty for an interactive session.")
    parser.add_argument("--local", help="Use a local .xlsx instead of OneDrive.")
    parser.add_argument("--provider", choices=["gemini", "qwen"], help="Override the model.")
    parser.add_argument("--refresh", action="store_true", help="Force a re-download.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        info = ensure_ready(local_path=args.local, force=args.refresh)
    except Exception as exc:
        print(f"\nCouldn't load the data: {exc}")
        print("Check the Graph settings in .env, or pass --local /path/to/Cash_Flow_Dash.xlsx")
        raise SystemExit(1)
    print(f"Ready: {info['rows']:,} rows, {info['date_min']} to {info['date_max']}\n")

    if args.provider:
        from .config import LLM
        LLM.provider = args.provider

    try:
        agent = CashFlowAgent()
    except LLMUnavailable as exc:
        print(f"\nModel not ready: {exc}")
        print("Set GEMINI_API_KEY in .env, or use --provider qwen with `ollama serve` running.")
        raise SystemExit(1)

    def show(turn: Turn) -> None:
        print(turn.answer)
        if turn.sql:
            print(f"\n--- SQL ---\n{turn.sql}\n")

    if args.question:
        try:
            show(agent.ask(" ".join(args.question)))
        except LLMUnavailable as exc:
            print(f"\nModel not reachable: {exc}")
            raise SystemExit(1)
        return

    print("Ask a question, or 'exit' to quit.")
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in {"exit", "quit"}:
            break
        if q:
            try:
                show(agent.ask(q))
            except LLMUnavailable as exc:
                print(f"Model not reachable: {exc}")


if __name__ == "__main__":
    main()
