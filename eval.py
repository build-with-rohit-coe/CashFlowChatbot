"""Compare backends on a fixed question set.  python eval.py --providers gemini qwen

Scoring is deliberately mechanical: for each question there's a reference SQL whose
result set is treated as truth, and the generated query passes if its numbers match.
That measures the thing that matters (right answer) rather than string-matching SQL,
which has a hundred correct spellings.

Run this before committing to a model. On this schema Qwen2.5:7b tends to hold up on
single-filter aggregates and slip on multi-step questions - measure it, don't guess.
"""
from __future__ import annotations

import argparse
import time

import pandas as pd

from cfchat.agent import CashFlowAgent, is_effectively_empty
from cfchat.config import LLM
from cfchat.database import run_query
from cfchat.llm import get_backend
from cfchat.pipeline import ensure_ready

GOLD: list[tuple[str, str]] = [
    ("How many transactions are in the data?",
     "SELECT COUNT(*) AS n FROM cash_flow"),

    ("What is the total expense across all time?",
     "SELECT SUM(amount) AS t FROM cash_flow WHERE flow_type='EXPENSE'"),

    ("What is total money received?",
     "SELECT SUM(amount) AS t FROM cash_flow WHERE flow_type='RECEIPT'"),

    ("What is the net cash flow overall?",
     "SELECT SUM(signed_amount) AS t FROM cash_flow"),

    ("Which team spent the most?",
     "SELECT team, SUM(amount) AS t FROM cash_flow WHERE flow_type='EXPENSE' "
     "GROUP BY team ORDER BY t DESC LIMIT 1"),

    ("Top 5 vendors by total payment",
     "SELECT party_name, SUM(amount) AS t FROM cash_flow WHERE flow_type='EXPENSE' "
     "GROUP BY party_name ORDER BY t DESC LIMIT 5"),

    ("How much did we spend on salaries and HR in FY 2025-26?",
     "SELECT SUM(amount) AS t FROM cash_flow WHERE flow_type='EXPENSE' "
     "AND exp_type='SALARY & OTHER HR EXP.' AND fiscal_year='2025-26'"),

    ("Total expense on EPC projects",
     "SELECT SUM(amount) AS t FROM cash_flow WHERE flow_type='EXPENSE' "
     "AND project_type='EPC PROECTS'"),

    ("Monthly total expense for fiscal year 2025-26",
     "SELECT month_key, SUM(amount) AS t FROM cash_flow WHERE flow_type='EXPENSE' "
     "AND fiscal_year='2025-26' GROUP BY month_key ORDER BY month_key"),

    ("Which bank account had the highest outflow?",
     "SELECT bank, SUM(amount) AS t FROM cash_flow WHERE flow_type='EXPENSE' "
     "GROUP BY bank ORDER BY t DESC LIMIT 1"),

    ("How much has been spent on the Dungarpur projects?",
     "SELECT SUM(amount) AS t FROM cash_flow WHERE flow_type='EXPENSE' "
     "AND project_std LIKE '%DUNGARPUR%'"),

    ("Net cash flow per partner",
     "SELECT partner, SUM(signed_amount) AS t FROM cash_flow GROUP BY partner"),

    ("What was the single largest payment, and to whom?",
     "SELECT party_name, amount FROM cash_flow WHERE flow_type='EXPENSE' "
     "ORDER BY amount DESC LIMIT 1"),

    ("Compare receipts to expenses for the PAANI team",
     "SELECT flow_type, SUM(amount) AS t FROM cash_flow WHERE team='PAANI' GROUP BY flow_type"),
]


def numeric_signature(df: pd.DataFrame) -> set:
    """Rounded numbers in the result, order-independent. Column naming is free."""
    if df is None or is_effectively_empty(df):
        return set()
    out = set()
    for col in df.select_dtypes("number").columns:
        for v in df[col].dropna():
            out.add(round(float(v), 2))
    return out


def grade(expected: set, got: set) -> str:
    if not expected:
        return "SKIP"
    if not got:
        return "EMPTY"
    if expected == got:
        return "PASS"
    if expected <= got:
        return "PASS+"        # right numbers, plus extra columns - still useful
    if expected & got:
        return "PARTIAL"
    return "FAIL"


def run(provider: str) -> pd.DataFrame:
    LLM.provider = provider
    agent = CashFlowAgent(backend=get_backend(LLM))
    rows = []
    for question, ref_sql in GOLD:
        expected = numeric_signature(run_query(ref_sql))
        t0 = time.perf_counter()
        turn = agent.ask(question)
        elapsed = time.perf_counter() - t0
        agent.history.clear()          # each question judged independently
        rows.append({
            "question": question[:52],
            "grade": grade(expected, numeric_signature(turn.rows)),
            "tries": len(turn.attempts),
            "secs": round(elapsed, 1),
        })
        print(f"  {rows[-1]['grade']:<7} {rows[-1]['secs']:>5}s  {question[:60]}")
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--providers", nargs="+", default=["gemini"], choices=["gemini", "qwen"])
    parser.add_argument("--local", help="Local .xlsx instead of OneDrive.")
    args = parser.parse_args()

    ensure_ready(local_path=args.local)
    for provider in args.providers:
        print(f"\n=== {provider} ===")
        df = run(provider)
        passed = df["grade"].isin(["PASS", "PASS+"]).sum()
        print(f"\n{provider}: {passed}/{len(df)} correct · "
              f"median {df['secs'].median():.1f}s · "
              f"{df['tries'].mean():.2f} attempts/question")
        bad = df[~df["grade"].isin(["PASS", "PASS+"])]
        if not bad.empty:
            print("Missed:")
            print(bad.to_string(index=False))


if __name__ == "__main__":
    main()
