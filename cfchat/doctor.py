"""Config check:  python -m cfchat.doctor

Reports what your .env actually resolves to, which data source will be used,
and whether the model is reachable - without running a query. Secrets are
masked, so the output is safe to paste when asking for help.
"""
from __future__ import annotations

from pathlib import Path

from .config import GRAPH, LLM, CACHE_DIR, DB_PATH, SHEET_NAME, XLSX_CACHE, env

OK, WARN, BAD, INFO = "[ ok ]", "[warn]", "[FAIL]", "[info]"


def mask(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]} ({len(value)} chars)"


def main() -> None:
    problems = 0
    print("=" * 62)
    print(" Cash Flow Assistant - configuration check")
    print("=" * 62)

    # ---------------- data source
    local = env("CFCHAT_LOCAL_XLSX")
    print("\nDATA SOURCE")
    if local:
        p = Path(local)
        print(f"  {INFO} Mode: LOCAL FILE (Graph API is not used)")
        print(f"         CFCHAT_LOCAL_XLSX = {local}")
        if p.exists():
            print(f"  {OK} File found, {p.stat().st_size / 1e6:.1f} MB")
            if "onedrive" in str(p).lower():
                print(f"  {OK} Path is inside a OneDrive folder - the desktop sync app "
                      f"keeps this current")
            else:
                print(f"  {WARN} Not inside a OneDrive folder, so this copy will go stale. "
                      f"Point it at your synced OneDrive folder, or clear it to use Graph.")
        else:
            print(f"  {BAD} File not found at that path.")
            problems += 1
        print(f"  {INFO} To switch to the Graph API, set CFCHAT_LOCAL_XLSX= (empty)")
    else:
        print(f"  {INFO} Mode: MICROSOFT GRAPH")
        print(f"         MS_AUTH_MODE   = {GRAPH.auth_mode}")
        print(f"         MS_CLIENT_ID   = {mask(GRAPH.client_id)}")
        print(f"         MS_TENANT_ID   = {mask(GRAPH.tenant_id)}")

        if GRAPH.auth_mode not in ("device", "app"):
            print(f"  {BAD} MS_AUTH_MODE must be 'device' or 'app'.")
            problems += 1
        if not GRAPH.client_id:
            print(f"  {BAD} MS_CLIENT_ID is empty - register an app in Entra ID first.")
            problems += 1
        if not GRAPH.tenant_id:
            print(f"  {WARN} MS_TENANT_ID is empty; falling back to 'common', which fails "
                  f"for most work accounts. Paste your Directory (tenant) ID.")
        if GRAPH.auth_mode == "app":
            print(f"         MS_CLIENT_SECRET = {mask(GRAPH.client_secret)}")
            if not GRAPH.client_secret:
                print(f"  {BAD} app mode needs MS_CLIENT_SECRET.")
                problems += 1
            print(f"  {INFO} app mode also needs Files.Read.All as an APPLICATION "
                  f"permission with admin consent granted.")
        else:
            print(f"  {INFO} device mode: you sign in at microsoft.com/devicelogin. "
                  f"No password is stored here.")
            print(f"  {INFO} Requires 'Allow public client flows: Yes' in the app's "
                  f"Authentication blade.")

        locators = {
            "MS_SHARE_LINK": GRAPH.share_link,
            "MS_ITEM_PATH": GRAPH.item_path,
        }
        given = {k: v for k, v in locators.items() if v}
        print(f"\n  File locator:")
        for k, v in locators.items():
            print(f"         {k:<15}= {v or '(empty)'}")
        if not given:
            print(f"  {BAD} Set MS_SHARE_LINK or MS_ITEM_PATH so it knows which file to read.")
            problems += 1
        elif len(given) == 2:
            print(f"  {WARN} Both are set. MS_SHARE_LINK wins; clear it to use the path.")
        else:
            print(f"  {OK} Using {next(iter(given))}")
        if GRAPH.share_link and not GRAPH.share_link.lower().startswith("http"):
            print(f"  {BAD} MS_SHARE_LINK is not a URL. If it starts with '#', you left an "
                  f"inline comment after an empty value - put comments on their own line.")
            problems += 1
        if GRAPH.item_path and "\\" in GRAPH.item_path:
            print(f"  {BAD} MS_ITEM_PATH must use forward slashes and be relative to the "
                  f"drive root, not a Windows path.")
            problems += 1
        if GRAPH.token_cache.exists():
            print(f"  {OK} Cached sign-in found - you won't be prompted again")

    # ---------------- model
    print("\nMODEL")
    print(f"  {INFO} CFCHAT_LLM = {LLM.provider}")
    if LLM.provider in ("gemini", "google"):
        print(f"         GEMINI_MODEL   = {LLM.gemini_model}")
        print(f"         GEMINI_API_KEY = {mask(LLM.gemini_api_key)}")
        if not LLM.gemini_api_key:
            print(f"  {BAD} GEMINI_API_KEY is empty - get one at aistudio.google.com/apikey")
            problems += 1
        else:
            print(f"  {OK} Key present")
            print(f"  {WARN} Questions, schema and result rows are sent to Google. "
                  f"Use CFCHAT_LLM=qwen to keep everything local.")
    else:
        print(f"         OLLAMA_HOST  = {LLM.ollama_host}")
        print(f"         OLLAMA_MODEL = {LLM.ollama_model}")
        try:
            import requests

            tags = requests.get(f"{LLM.ollama_host.rstrip('/')}/api/tags", timeout=5).json()
            names = [m["name"] for m in tags.get("models", [])]
            print(f"  {OK} Ollama is running. Models: {', '.join(names) or 'none'}")
            if not any(n.startswith(LLM.ollama_model.split(':')[0]) for n in names):
                print(f"  {BAD} {LLM.ollama_model} not pulled. Run: "
                      f"ollama pull {LLM.ollama_model}")
                problems += 1
        except Exception as exc:
            print(f"  {BAD} Can't reach Ollama: {type(exc).__name__}. Run `ollama serve`.")
            problems += 1

    # ---------------- local state
    print("\nLOCAL STATE")
    print(f"  {INFO} Cache folder : {CACHE_DIR}")
    print(f"  {INFO} Sheet read   : {SHEET_NAME}")
    print(f"  {'[ ok ]' if XLSX_CACHE.exists() else '[info]'} Cached workbook: "
          f"{'present' if XLSX_CACHE.exists() else 'not downloaded yet'}")
    if DB_PATH.exists():
        try:
            from .database import connect, TABLE_NAME

            conn = connect()
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0]
                lo, hi = conn.execute(
                    f"SELECT MIN(txn_date), MAX(txn_date) FROM {TABLE_NAME}"
                ).fetchone()
                print(f"  {OK} Database   : {n:,} rows, {lo} to {hi}")
            finally:
                conn.close()
        except Exception as exc:
            print(f"  {WARN} Database exists but won't open ({exc}). "
                  f"Delete {CACHE_DIR} to rebuild.")
    else:
        print(f"  {INFO} Database   : not built yet (happens on first run)")

    print("\n" + "=" * 62)
    if problems:
        print(f" {problems} problem(s) to fix above.")
        raise SystemExit(1)
    print(" Configuration looks good. Try:")
    print("   python -m cfchat.agent \"what was total spend last month?\"")


if __name__ == "__main__":
    main()
