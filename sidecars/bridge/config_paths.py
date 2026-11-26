"""Shared paths and atomic, lock-safe JSON storage for the WeChat Assistant plugin."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# sidecars/bridge/config_paths.py -> parents[2] is the plugin root directory
SIDECAR_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = SIDECAR_DIR.parent.parent
PLUGIN_ID = PLUGIN_DIR.name or "wechat-assistant"
SIDECAR_NAME = SIDECAR_DIR.name or "bridge"
SIDECAR_ID = f"{PLUGIN_ID}/{SIDECAR_NAME}"

# Unify runtime storage across sidecar (bridge.py), Stop hook (stop_hook.py),
# MCP server (wechat-mcp-server.mjs), QR login (login.py), and setup wizard (setup.py).
# Using ~/.gemini/antigravity/plugin_data/<plugin_dir_name> matches Antigravity's
# $PLUGIN_DATA so every component shares the exact same account.json & state.json.
DEFAULT_DATA_DIR = Path.home() / ".gemini" / "antigravity" / "plugin_data" / PLUGIN_ID
DATA_DIR = Path(
    os.environ.get("WECHAT_ASSISTANT_DATA_DIR")
    or os.environ.get("PLUGIN_DATA")
    or DEFAULT_DATA_DIR
)
DATA_DIR.mkdir(parents=True, exist_ok=True)

VENV_DIR = DATA_DIR / "venv"
SILK_LOCAL_DIR = DATA_DIR / "silk"
WEFLOW_LOCAL_DIR = DATA_DIR / "weflow"
ACCOUNT_FILE = DATA_DIR / "account.json"
STATE_FILE = DATA_DIR / "state.json"
STATE_LOCK_FILE = DATA_DIR / "state.lock"
BRIDGE_LOCK_FILE = DATA_DIR / "bridge.lock"
HOOK_LOG_FILE = DATA_DIR / "hook.log"
QR_IMAGE_FILE = DATA_DIR / "qrcode.png"

DEFAULT_INBOX_DIR = Path(
    os.environ.get("WECHAT_ASSISTANT_INBOX_DIR") or (Path.home() / "WeChatInbox")
)
DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"


def get_venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def default_state() -> dict[str, Any]:
    return {
        "get_updates_buf": "",
        "active_conversation_id": "",
        "model": "",
        "inbox_dir": str(DEFAULT_INBOX_DIR),
        "default_project_id": "",
        "target_project_id": "",
        "target_project_name": "Default",
        "digest_auto_push": True,
        "pending_startup_dispatch": None,
        "throttle_until": 0.0,
        "last_send_ts": 0.0,
        "peers": {},
        "tracked_conversations": {},
        "watched_conversations": {},
        "processed_msg_ids": [],
        "last_listed_sessions": [],
        "last_listed_projects": [],
    }


@contextmanager
def file_lock(lock_path: Path, timeout_sec: float = 10.0) -> Iterator[None]:
    """Cross-process exclusive lock using msvcrt on Windows or fcntl on POSIX."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    deadline = time.monotonic() + timeout_sec
    locked = False
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    if fh.tell() == 0 and lock_path.stat().st_size == 0:
                        fh.write(b"0")
                        fh.flush()
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for lock {lock_path}")
                time.sleep(0.05)
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: Path, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(fallback) if fallback is not None else {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return dict(fallback) if fallback is not None else {}


def load_account() -> dict[str, Any] | None:
    acc = read_json(ACCOUNT_FILE)
    if acc.get("bot_token"):
        return acc
    return None


def save_account(account: dict[str, Any]) -> None:
    with file_lock(STATE_LOCK_FILE):
        atomic_write_json(ACCOUNT_FILE, account)


def load_state() -> dict[str, Any]:
    base = default_state()
    data = read_json(STATE_FILE)
    base.update(data)
    return base


@contextmanager
def locked_state() -> Iterator[dict[str, Any]]:
    """Load state under an exclusive lock and atomically persist changes on exit."""
    with file_lock(STATE_LOCK_FILE):
        state = load_state()
        yield state
        atomic_write_json(STATE_FILE, state)
