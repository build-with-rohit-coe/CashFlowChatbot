"""Central configuration. Everything is env-driven so nothing secret lives in code."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def env(name: str, default: str = "") -> str:
    """Read an env var, tolerating the commonest .env mistakes.

    python-dotenv strips a trailing comment only when there is a value before it,
    so `MS_SHARE_LINK=      # paste here` yields the comment text as the value.
    Anything that is blank or starts with '#' is treated as unset.
    """
    raw = os.getenv(name, default) or ""
    raw = raw.strip()
    if raw.startswith("#"):
        return ""
    return raw

CACHE_DIR = Path(env("CFCHAT_CACHE_DIR") or Path.home() / ".cache" / "cfchat")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

XLSX_CACHE = CACHE_DIR / "Cash_Flow_Dash.xlsx"
DB_PATH = CACHE_DIR / "cashflow.db"
META_PATH = CACHE_DIR / "source_meta.json"

SHEET_NAME = env("CFCHAT_SHEET", "Details")
TABLE_NAME = "cash_flow"

# How long to trust the local copy before asking Graph whether the file changed.
REFRESH_SECONDS = int(env("CFCHAT_REFRESH_SECONDS", "300"))


@dataclass
class GraphConfig:
    """Microsoft Graph / OneDrive settings.

    Two auth modes:
      * "app"       - client credentials. Unattended, needs Files.Read.All
                      application permission + admin consent. Use for a shared
                      OneDrive for Business / SharePoint file.
      * "device"    - device code flow. Interactive once, then the token cache
                      keeps you signed in. Use for a personal or user-owned file.
    """

    tenant_id: str = field(default_factory=lambda: env("MS_TENANT_ID", ""))
    client_id: str = field(default_factory=lambda: env("MS_CLIENT_ID", ""))
    client_secret: str = field(default_factory=lambda: env("MS_CLIENT_SECRET", ""))
    auth_mode: str = field(default_factory=lambda: env("MS_AUTH_MODE", "device"))

    # Locate the workbook. Either give a share link, or drive_id + item path.
    share_link: str = field(default_factory=lambda: env("MS_SHARE_LINK", ""))
    drive_id: str = field(default_factory=lambda: env("MS_DRIVE_ID", ""))
    item_path: str = field(default_factory=lambda: env("MS_ITEM_PATH", ""))
    user_id: str = field(default_factory=lambda: env("MS_USER_ID", ""))

    token_cache: Path = field(default_factory=lambda: CACHE_DIR / "msal_cache.json")

    @property
    def authority(self) -> str:
        tenant = self.tenant_id or "common"
        return f"https://login.microsoftonline.com/{tenant}"

    @property
    def scopes(self) -> list[str]:
        if self.auth_mode == "app":
            return ["https://graph.microsoft.com/.default"]
        return ["Files.Read.All"]


@dataclass
class LLMConfig:
    """Which model answers the questions.

    provider="gemini" -> Google Gemini API (needs GEMINI_API_KEY)
    provider="qwen"   -> Qwen2.5:7b served locally by Ollama
    """

    provider: str = field(default_factory=lambda: env("CFCHAT_LLM", "gemini"))
    gemini_api_key: str = field(default_factory=lambda: env("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: env("GEMINI_MODEL", "gemini-2.5-flash"))
    ollama_host: str = field(default_factory=lambda: env("OLLAMA_HOST", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: env("OLLAMA_MODEL", "qwen2.5:7b"))
    temperature: float = 0.0


GRAPH = GraphConfig()
LLM = LLMConfig()

MAX_ROWS_RETURNED = 500      # hard LIMIT stapled onto every generated query
MAX_ROWS_TO_MODEL = 40       # rows shown to the LLM when it writes the prose answer
SQL_TIMEOUT_SECONDS = 15
MAX_REPAIR_ATTEMPTS = 2
