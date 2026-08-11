# Cash Flow Assistant

Ask questions in plain English about the `Details` sheet of `Cash_Flow_Dash.xlsx`.
The workbook stays on OneDrive; this pulls it, mirrors it into SQLite, and lets
Gemini or a local Qwen2.5:7b write the SQL.

```
OneDrive ──Graph API──> cached .xlsx ──pandas──> SQLite (read-only)
                                                    │
                    question ──> LLM writes SQL ────┤
                                      ▲             ▼
                                      └── error / value hints ── execute
                                                                  │
                                                     rows ──> LLM writes the answer
```

## Why SQL and not a RAG index

The questions here are aggregations — "total spend last month", "top 10 vendors".
Vector search over 24,726 rows retrieves *some* rows and the model adds up whatever
it happened to get, which produces confident wrong totals. SQL computes over all
rows, and the query is visible so a finance manager can check it. Every answer in
the UI ships with its query behind a "Show the query" expander.

Handing the model a pandas DataFrame and letting it write Python is the other common
approach. It's more flexible and strictly less safe: it needs `exec`. SQL against a
read-only connection can't do anything worse than return the wrong rows.

## Setup

Step-by-step instructions with screenshots-level detail are in **QUICKSTART.md**.
The short version:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill it in (see QUICKSTART.md)
```

Test it without touching Graph first:

```bash
export CFCHAT_LOCAL_XLSX=/path/to/Cash_Flow_Dash.xlsx
python -m cfchat.agent --local "$CFCHAT_LOCAL_XLSX" "top 5 vendors this fiscal year"
streamlit run app.py
```

Check your config at any time:

```bash
python -m cfchat.doctor
```

### Two ways to read from OneDrive

**Synced folder (no Azure setup).** If the OneDrive desktop app syncs the folder, the
local path *is* the OneDrive file — edits from anyone sync down automatically. Set
`CFCHAT_LOCAL_XLSX` to the path inside your `OneDrive - <Company>` folder and you're
done. No app registration, no admin approval.

**Graph API (no desktop sync needed).** Reads the file directly from Microsoft's
servers. Needs an app registration, below. Leave `CFCHAT_LOCAL_XLSX` empty to enable it.

### Graph API access

Register an app in Entra ID (Azure AD) → App registrations, then choose one mode:

| | `MS_AUTH_MODE=device` | `MS_AUTH_MODE=app` |
|---|---|---|
| Permission type | Delegated `Files.Read.All` | Application `Files.Read.All` + admin consent |
| Sign-in | Once, interactive; token cached | None |
| Fits | Your own OneDrive, one user | Shared/service deployment |
| Secret needed | No | Yes (`MS_CLIENT_SECRET`) |

Then point it at the file with **one** of:

- `MS_SHARE_LINK` — paste the OneDrive "Copy link" URL. Easiest.
- `MS_ITEM_PATH=Documents/Cash_Flow_Dash.xlsx`, optionally with `MS_DRIVE_ID`
  (for a SharePoint document library) or `MS_USER_ID` (app mode, someone else's drive).

The sync checks the item's `eTag` and `lastModifiedDateTime` before downloading, so a
chat turn normally costs one small metadata call. The 3.4 MB download and the Excel
parse only happen when the workbook actually changed. `CFCHAT_REFRESH_SECONDS`
(default 300) is how long the local copy is trusted without even asking.

### Model

```bash
# Gemini
CFCHAT_LLM=gemini
GEMINI_API_KEY=...

# or Qwen2.5:7b, fully local
ollama pull qwen2.5:7b
CFCHAT_LLM=qwen
```

## Which model

Run the eval before you commit — it grades generated queries against reference SQL
by comparing the numbers, not the query text:

```bash
python eval.py --providers gemini qwen --local /path/to/Cash_Flow_Dash.xlsx
```

Broadly what to expect:

**Gemini Flash** handles multi-step questions ("compare this quarter to last, by
team") and is the safer default for accuracy. It means the schema, the question, and
the result rows leave your network — for a cash-flow ledger with named vendors, that
may be the deciding factor regardless of accuracy.

**Qwen2.5:7b** keeps everything on your machine and is solid on the shape of question
this schema is built for: one or two filters plus an aggregate. It degrades on
correlated subqueries and on anything needing several joins of logic. Two things in
this repo exist mainly to prop it up — the enumerated value lists in the prompt and
the repair loop — and they close much of the gap. If it's still short, `qwen2.5:14b`
or `qwen2.5-coder:7b` are the next things to try; the coder variant is often better at
SQL than the general model at the same size.

## What makes this accurate

The hard part of text-to-SQL over real business data isn't SQL, it's values. This
sheet contains `EPC PROECTS` — a typo, in every row — plus non-breaking spaces inside
category names and `Amount` stored positive with the direction in a separate column.
A model asked about "EPC projects" writes `project_type = 'EPC PROJECTS'`, gets zero
rows, and reports ₹0. Four countermeasures:

1. **Cleaning on load** (`database.py`) — NFKC normalise, strip NBSP, collapse whitespace.
2. **Derived columns** — `month_key` (sortable, unlike `Apr 2025`), `fiscal_year`
   (April–March), `quarter`, and `signed_amount` (+receipt/−expense) so "net cash
   flow" is `SUM(signed_amount)` rather than a `CASE` the model must remember.
3. **Every categorical value pasted into the prompt** (`schema.py`), typos included,
   with an explicit instruction to keep them verbatim.
4. **Repair loop** (`agent.py`) — on a SQL error, or on zero rows, literals are
   fuzzy-matched against real values and the model retries with the correction as a
   hint. `'RECIEPT'` → "closest real value is `'RECEIPT'` in column flow_type".

An aggregate over zero matching rows returns one row of `NULL`, not an empty
result — `is_effectively_empty()` catches that, otherwise `SUM()` over no matches
gets reported as a real total.

## Layout

| File | Job |
|---|---|
| `cfchat/onedrive.py` | Graph auth, ETag-cached download |
| `cfchat/fsutil.py` | Cross-platform atomic replace + SQLite URI building |
| `cfchat/doctor.py` | `python -m cfchat.doctor` - config check, secrets masked |
| `cfchat/database.py` | Excel → SQLite, cleaning, SQL guard rails |
| `cfchat/schema.py` | The schema card the model sees; value grounding |
| `cfchat/llm.py` | Gemini and Ollama backends behind one interface |
| `cfchat/agent.py` | Plan → execute → repair → summarise; also a CLI |
| `cfchat/pipeline.py` | Sync + rebuild, skipping unnecessary work |
| `app.py` | Streamlit chat UI |
| `eval.py` | Graded question set for comparing models |

## Guard rails

Generated SQL must be a single `SELECT` or `WITH`; write and schema keywords are
rejected; a `LIMIT 500` is stapled on if missing; the connection is opened
`mode=ro`; a progress handler interrupts anything running past 15 seconds.

## Windows notes

Two things differ from POSIX and are handled in `fsutil.py`:

- **Renaming onto an open file** fails with `WinError 32`. The database is built to
  a temp file and moved into place, so every rename retries with a backoff and then
  falls back to an in-place byte copy. If you still see it, close any second copy of
  the app and retry; `rm -rf %USERPROFILE%\.cache\cfchat` clears all state.
- **SQLite URI paths** need forward slashes and percent-encoding, so read-only
  connections are opened via `Path.as_uri()` rather than string interpolation.

## Known limits

- `Purpose` is empty in all 24,728 source rows and is dropped on load.
- Two rows have a category but no amount or flow type; they're dropped (24,726 load).
- Only the `Details` sheet is read. The pivot and dashboard sheets are derived from
  it, so nothing is lost, but if a dashboard figure disagrees with an answer here,
  check whether the dashboard applies a filter this doesn't know about.
- One transaction has a negative `Amount`, which inverts its direction relative to
  `flow_type`. Worth fixing at source.
- No user-level permissions: anyone who can open the app sees the whole ledger.
