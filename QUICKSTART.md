# Quickstart

Two stages. Do stage 1 first — it proves the SQL brain works before you fight
with Microsoft app registration.

---

## Stage 1 — Run it offline against a local copy (10 minutes)

```bash
# 1. Unzip and enter
unzip cashflow-chat.zip && cd cashflow-chat

# 2. Virtual environment
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# 3. Dependencies
pip install -r requirements.txt

# 4. Config
cp .env.example .env
```

Open `.env` and set just two things:

```ini
CFCHAT_LLM=gemini
GEMINI_API_KEY=your_key_from_aistudio.google.com
CFCHAT_LOCAL_XLSX=/full/path/to/Cash_Flow_Dash.xlsx
```

Or for the local model instead:

```bash
ollama pull qwen2.5:7b               # ~4.7 GB
ollama serve                         # leave running in its own terminal
```
```ini
CFCHAT_LLM=qwen
CFCHAT_LOCAL_XLSX=/full/path/to/Cash_Flow_Dash.xlsx
```

Test from the command line:

```bash
python -m cfchat.agent --local "$CFCHAT_LOCAL_XLSX" "top 5 vendors this fiscal year"
```

You should see a prose answer plus the SQL it ran. Then launch the chat UI:

```bash
streamlit run app.py                 # opens http://localhost:8501
```

Check which model to trust before you go further:

```bash
python eval.py --providers gemini qwen --local "$CFCHAT_LOCAL_XLSX"
```

---

## Stage 2 — Switch the source to OneDrive

### 2a. Register an app

1. [portal.azure.com](https://portal.azure.com) → **Microsoft Entra ID** →
   **App registrations** → **New registration**
2. Name it anything. Supported account types: **Accounts in this organizational
   directory only**.
3. Under **Redirect URI**, leave it blank for now.
4. Copy the **Application (client) ID** and **Directory (tenant) ID** from the
   Overview page.

### 2b. Grant permission

**API permissions** → **Add a permission** → **Microsoft Graph**:

- *Interactive, single user (simplest):* **Delegated permissions** →
  `Files.Read.All`. Then **Authentication** → **Allow public client flows: Yes**.
- *Unattended / shared deployment:* **Application permissions** →
  `Files.Read.All`, then **Grant admin consent** (needs an admin). Also create a
  secret under **Certificates & secrets** → **New client secret**.

### 2c. Point it at the workbook

In OneDrive, open the folder containing `Cash_Flow_Dash.xlsx`, click
**Share → Copy link**, and paste it into `.env`.

```ini
# remove or comment out CFCHAT_LOCAL_XLSX
MS_AUTH_MODE=device                  # or: app
MS_CLIENT_ID=<Application (client) ID>
MS_TENANT_ID=<Directory (tenant) ID>
MS_CLIENT_SECRET=                    # only for MS_AUTH_MODE=app
MS_SHARE_LINK=https://...sharepoint.com/:x:/g/personal/...
```

Instead of a share link you can use a path: `MS_ITEM_PATH=Documents/Cash_Flow_Dash.xlsx`.

### 2d. First run

```bash
python -m cfchat.agent "how many transactions are there?"
```

In `device` mode it prints a code and a URL once. Open it, sign in, approve. The
token is cached in `~/.cache/cfchat/` so you won't be asked again.

```bash
streamlit run app.py
```

---

## Everyday use

```bash
source .venv/bin/activate
streamlit run app.py
```

The workbook re-downloads only when its OneDrive `eTag` changes. **Refresh from
OneDrive** in the sidebar forces a check immediately.

---

## If something breaks

| Symptom | Cause |
|---|---|
| `GEMINI_API_KEY is not set` | `.env` not saved, or you're running from a different directory |
| `Could not reach Ollama` | `ollama serve` isn't running |
| `Auth failed: invalid_client` | Wrong `MS_CLIENT_ID`, or public client flows not enabled (device mode) |
| `Graph returned 404` | Bad `MS_ITEM_PATH`. Path is relative to the drive root, no leading slash |
| `AADSTS65001` consent error | App-mode permission never got admin consent |
| Answers look wrong | Open **Show the query** — the SQL tells you exactly what it measured |
| Numbers changed after an edit | Expected; it re-read the workbook. Sidebar refresh forces it |
| `WinError 32 ... used by another process` | Another copy of the app is running. Close it and retry |

Clear all cached state (workbook, database, sign-in token):

```bash
rm -rf ~/.cache/cfchat                       # macOS / Linux
rmdir /s /q %USERPROFILE%\.cache\cfchat      # Windows (cmd)
```
