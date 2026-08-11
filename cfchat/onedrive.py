"""Pull the workbook from OneDrive / SharePoint via Microsoft Graph.

The download is cached. On each check we ask Graph only for the item's
metadata (a few hundred bytes) and compare eTag + lastModifiedDateTime
against what we stored. The 3.4 MB file comes down only when it actually
changed, so a chat turn costs one cheap HEAD-ish call, not a full download.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import msal
import requests

from .config import GRAPH, META_PATH, REFRESH_SECONDS, XLSX_CACHE, GraphConfig
from .fsutil import atomic_replace, temp_sibling

log = logging.getLogger(__name__)
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


class OneDriveError(RuntimeError):
    pass


# ---------------------------------------------------------------- auth


def _acquire_token(cfg: GraphConfig) -> str:
    if cfg.auth_mode == "app":
        if not (cfg.tenant_id and cfg.client_id and cfg.client_secret):
            raise OneDriveError(
                "App-only auth needs MS_TENANT_ID, MS_CLIENT_ID and MS_CLIENT_SECRET."
            )
        app = msal.ConfidentialClientApplication(
            cfg.client_id, authority=cfg.authority, client_credential=cfg.client_secret
        )
        result = app.acquire_token_silent(cfg.scopes, account=None)
        if not result:
            result = app.acquire_token_for_client(scopes=cfg.scopes)
    else:
        cache = msal.SerializableTokenCache()
        if cfg.token_cache.exists():
            cache.deserialize(cfg.token_cache.read_text())
        app = msal.PublicClientApplication(
            cfg.client_id, authority=cfg.authority, token_cache=cache
        )
        accounts = app.get_accounts()
        result = app.acquire_token_silent(cfg.scopes, account=accounts[0]) if accounts else None
        if not result:
            flow = app.initiate_device_flow(scopes=cfg.scopes)
            if "user_code" not in flow:
                raise OneDriveError(f"Could not start device flow: {flow}")
            print(flow["message"], flush=True)   # "Go to microsoft.com/devicelogin and enter CODE"
            result = app.acquire_token_by_device_flow(flow)
        if cache.has_state_changed:
            cfg.token_cache.write_text(cache.serialize())
            cfg.token_cache.chmod(0o600)

    if "access_token" not in result:
        raise OneDriveError(
            f"Auth failed: {result.get('error')} - {result.get('error_description')}"
        )
    return result["access_token"]


# ---------------------------------------------------------------- item location


def _item_url(cfg: GraphConfig) -> str:
    """Build the Graph URL for the workbook from whichever locator is configured."""
    if cfg.share_link:
        if not cfg.share_link.lower().startswith("http"):
            raise OneDriveError(
                f"MS_SHARE_LINK does not look like a URL: {cfg.share_link!r}. "
                "Paste the full https://... link from OneDrive's Share > Copy link."
            )
        encoded = base64.urlsafe_b64encode(cfg.share_link.encode()).decode().rstrip("=")
        return f"{GRAPH_ROOT}/shares/u!{encoded}/driveItem"
    if cfg.item_path and "\\" in cfg.item_path:
        raise OneDriveError(
            f"MS_ITEM_PATH must be a OneDrive path with forward slashes, relative to "
            f"the drive root - not a Windows path. Got: {cfg.item_path!r}"
        )
    if cfg.drive_id and cfg.item_path:
        return f"{GRAPH_ROOT}/drives/{cfg.drive_id}/root:/{cfg.item_path.lstrip('/')}"
    if cfg.user_id and cfg.item_path:
        return f"{GRAPH_ROOT}/users/{cfg.user_id}/drive/root:/{cfg.item_path.lstrip('/')}"
    if cfg.item_path:
        return f"{GRAPH_ROOT}/me/drive/root:/{cfg.item_path.lstrip('/')}"
    raise OneDriveError(
        "Set MS_SHARE_LINK, or MS_ITEM_PATH (optionally with MS_DRIVE_ID / MS_USER_ID)."
    )


@dataclass
class SourceMeta:
    etag: str
    last_modified: str
    size: int
    checked_at: float

    def to_json(self) -> str:
        return json.dumps(self.__dict__)


def _load_meta() -> SourceMeta | None:
    if not META_PATH.exists():
        return None
    try:
        return SourceMeta(**json.loads(META_PATH.read_text()))
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------- public API


def sync_workbook(force: bool = False) -> tuple[Path, bool]:
    """Make sure the local copy is current.

    Returns (path_to_xlsx, changed) where `changed` is True when fresh bytes
    landed on disk and the SQLite database therefore needs rebuilding.
    """
    meta = _load_meta()
    have_file = XLSX_CACHE.exists()

    if have_file and not force and meta and (time.time() - meta.checked_at) < REFRESH_SECONDS:
        return XLSX_CACHE, False

    token = _acquire_token(GRAPH)
    headers = {"Authorization": f"Bearer {token}"}

    # No $select here on purpose. `@microsoft.graph.downloadUrl` is an instance
    # annotation that Graph omits when a $select list is supplied, and the full
    # driveItem is only a few KB anyway.
    resp = requests.get(_item_url(GRAPH), headers=headers, timeout=30)
    if resp.status_code == 404:
        raise OneDriveError(
            "Graph returned 404 - the file wasn't found. Check MS_ITEM_PATH "
            "(relative to the drive root, no leading slash) or MS_SHARE_LINK."
        )
    if resp.status_code == 403:
        raise OneDriveError(
            "Graph returned 403 - authenticated but not authorised. The app "
            "registration needs Files.Read.All, and in app mode it needs admin consent."
        )
    resp.raise_for_status()
    item = resp.json()

    if "file" not in item and item.get("folder"):
        raise OneDriveError(
            f"That path points at a folder, not a file. Append the filename, "
            f"e.g. {GRAPH.item_path.rstrip('/')}/Cash_Flow_Dash.xlsx"
        )

    remote = SourceMeta(
        etag=item.get("eTag", ""),
        last_modified=item.get("lastModifiedDateTime", ""),
        size=int(item.get("size", 0)),
        checked_at=time.time(),
    )

    unchanged = (
        have_file
        and meta is not None
        and meta.etag == remote.etag
        and meta.last_modified == remote.last_modified
    )
    if unchanged and not force:
        META_PATH.write_text(remote.to_json())
        return XLSX_CACHE, False

    download_url = item.get("@microsoft.graph.downloadUrl")
    if not download_url:
        # Build the /content endpoint from the item's own identity. Works for a
        # share link too, where the request URL has no drive/item path in it.
        drive_id = (item.get("parentReference") or {}).get("driveId")
        item_id = item.get("id")
        if not (drive_id and item_id):
            raise OneDriveError(
                "Graph did not return a download URL and the item is missing its "
                "drive/item id, so the file can't be fetched."
            )
        download_url = f"{GRAPH_ROOT}/drives/{drive_id}/items/{item_id}/content"

    log.info("Downloading %s (%.1f MB)", item.get("name"), remote.size / 1e6)

    tmp = temp_sibling(XLSX_CACHE, "part")
    tmp.unlink(missing_ok=True)
    # The pre-authenticated downloadUrl must NOT carry our bearer token; the
    # /content fallback must. Send auth only when hitting graph.microsoft.com.
    dl_headers = headers if download_url.startswith(GRAPH_ROOT) else None
    with requests.get(download_url, headers=dl_headers, timeout=300, stream=True) as dl:
        dl.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in dl.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    atomic_replace(tmp, XLSX_CACHE)
    META_PATH.write_text(remote.to_json())
    return XLSX_CACHE, True


def use_local_file(path: str | Path) -> tuple[Path, bool]:
    """Escape hatch for development: point at a local .xlsx, skip Graph entirely."""
    src = Path(path).expanduser().resolve()
    if not src.exists():
        raise OneDriveError(f"No such file: {src}")
    changed = (
        not XLSX_CACHE.exists()
        or src.stat().st_mtime > XLSX_CACHE.stat().st_mtime
        or src.stat().st_size != XLSX_CACHE.stat().st_size
    )
    if changed:
        tmp = temp_sibling(XLSX_CACHE, "copy")
        tmp.write_bytes(src.read_bytes())
        atomic_replace(tmp, XLSX_CACHE)
    return XLSX_CACHE, changed
