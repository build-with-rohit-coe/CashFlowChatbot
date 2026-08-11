"""Streamlit chat UI.  Run with:  streamlit run app.py"""
from __future__ import annotations

import logging
import os

import pandas as pd
import streamlit as st

from cfchat.agent import CashFlowAgent
from cfchat.config import LLM
from cfchat.llm import LLMUnavailable, get_backend
from cfchat.pipeline import ensure_ready

logging.basicConfig(level=logging.INFO)

st.set_page_config(page_title="Cash Flow Assistant", page_icon="₹", layout="wide")

STARTERS = [
    "What was total spend last month?",
    "Top 10 vendors by payment this fiscal year",
    "Monthly net cash flow by team",
    "Which projects received the most money in FY 2025-26?",
]


@st.cache_resource(show_spinner=False)
def boot(provider: str, local_path: str | None):
    stats = ensure_ready(local_path=local_path)
    LLM.provider = provider
    return CashFlowAgent(backend=get_backend(LLM)), stats


with st.sidebar:
    st.subheader("Setup")
    provider = st.radio(
        "Model",
        ["gemini", "qwen"],
        index=0 if LLM.provider == "gemini" else 1,
        format_func=lambda p: "Gemini (hosted)" if p == "gemini" else "Qwen2.5:7b (local)",
        help="Gemini is more accurate on multi-step questions. Qwen keeps the data on your machine.",
    )
    local_path = os.getenv("CFCHAT_LOCAL_XLSX") or None
    if local_path:
        st.caption(f"Reading a local file: `{local_path}`")

    if st.button("Refresh from OneDrive", use_container_width=True):
        boot.clear()
        ensure_ready(local_path=local_path, force=True)
        st.success("Pulled the latest workbook.")

st.title("Cash Flow Assistant")

try:
    agent, stats = boot(provider, local_path)
except Exception as exc:
    st.error(f"Couldn't load the data: {exc}")
    st.info("Check your Microsoft Graph settings in `.env`, or set CFCHAT_LOCAL_XLSX to test offline.")
    st.stop()

st.caption(
    f"{stats['rows']:,} transactions · {stats['date_min']} to {stats['date_max']} · "
    f"answering with {'Gemini' if provider == 'gemini' else 'Qwen2.5:7b'}"
)

if "messages" not in st.session_state:
    st.session_state.messages = []

if not st.session_state.messages:
    st.write("Try one of these, or ask your own:")
    cols = st.columns(len(STARTERS))
    for col, starter in zip(cols, STARTERS):
        if col.button(starter, use_container_width=True):
            st.session_state.pending = starter
            st.rerun()

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("rows") is not None and len(msg["rows"]) > 0:
            st.dataframe(msg["rows"], use_container_width=True, hide_index=True)
        if msg.get("sql"):
            with st.expander("Show the query"):
                st.code(msg["sql"], language="sql")

question = st.chat_input("Ask about spend, receipts, projects, vendors, teams…")
if not question and "pending" in st.session_state:
    question = st.session_state.pop("pending")

if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        try:
            with st.spinner("Working out the query…"):
                turn = agent.ask(question)
        except LLMUnavailable as exc:
            st.error(f"Model not reachable: {exc}")
            st.stop()
        st.markdown(turn.answer)

        rows: pd.DataFrame | None = turn.rows
        if rows is not None and not rows.empty:
            st.dataframe(rows, use_container_width=True, hide_index=True)
            numeric = rows.select_dtypes("number").columns
            if 1 <= len(rows) <= 60 and len(numeric) == 1 and len(rows.columns) == 2:
                label = [c for c in rows.columns if c not in numeric][0]
                st.bar_chart(rows.set_index(label)[numeric[0]])
        if turn.sql:
            with st.expander("Show the query"):
                st.code(turn.sql, language="sql")
        if turn.error:
            st.warning(turn.error)

    st.session_state.messages.append(
        {"role": "assistant", "content": turn.answer, "sql": turn.sql, "rows": turn.rows}
    )
