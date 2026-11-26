"""Antigravity Sidecar daemon bridging WeChat ClawBot (iLink) to Antigravity 2.0 App."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config_paths import (
    ACCOUNT_FILE,
    BRIDGE_LOCK_FILE,
    DATA_DIR,
    DEFAULT_BASE_URL,
    DEFAULT_CDN_BASE_URL,
    DEFAULT_INBOX_DIR,
    SIDECAR_DIR,
    file_lock,
    load_account,
    load_state,
    locked_state,
)
from extras import (
    capture_desktop_screenshot,
    collect_system_status,
    find_latest_digest,
    format_digest_for_wechat,
    list_user_projects,
    resolve_project,
    switch_sidecar_project_in_config,
)
from ilink_client import (
    ILinkClient,
    ILinkError,
    ILinkSessionExpiredError,
    ILinkThrottledError,
    MSG_TYPE_USER,
    TYPING_START,
    TYPING_STOP,
)

DEBOUNCE_SECONDS = 1.8
TYPING_HEARTBEAT_INTERVAL = 20.0
TYPING_MAX_DURATION = 600.0
MAX_PROCESSED_IDS = 500
THROTTLE_COOLDOWN_SEC = 1800.0  # 30 min backoff on ret=-2

_SEND_REPLY_SCRIPT = str(SIDECAR_DIR / "send_reply.py")
BRAIN_DIR = Path.home() / ".gemini" / "antigravity" / "brain"
PROJECTS_DIR = Path.home() / ".gemini" / "config" / "projects"
_USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{6,64}$")


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [wechat-assistant] {msg}", flush=True)


def load_project_names() -> dict[str, str]:
    names: dict[str, str] = {"outside-of-project": "Outside of Project"}
    if not PROJECTS_DIR.exists():
        return names
    for pf in PROJECTS_DIR.glob("*.json"):
        try:
            obj = json.loads(pf.read_text(encoding="utf-8"))
            pid = str(obj.get("id") or pf.stem)
            name = str(obj.get("name") or pid[:8])
            names[pid] = name
        except Exception:
            continue
    return names


def extract_session_summary(transcript_path: Path) -> tuple[bool, str, int]:
    """Return (is_main_user_session, short_title, latest_planner_step_index)."""
    is_main = False
    summary = ""
    max_planner_step = -1
    target = transcript_path.with_name("transcript_full.jsonl")
    if not target.exists():
        target = transcript_path
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if idx == 0:
                    if entry.get("type") != "USER_INPUT":
                        return False, "", -1
                    is_main = True
                    raw_c = str(entry.get("content") or "")
                    m = _USER_REQ_RE.search(raw_c)
                    body = (m.group(1) if m else raw_c).strip()
                    # Strip [微信小助理消息 · ...] header line if present
                    body_lines = [
                        ln.strip()
                        for ln in body.splitlines()
                        if ln.strip()
                        and not ln.strip().startswith("[微信小助理消息")
                        and not ln.strip().startswith("（提醒：你的最终文本回复会自动推送")
                    ]
                    first_line = body_lines[0] if body_lines else body
                    summary = re.sub(r"\s+", " ", first_line).strip()[:32]
                if entry.get("type") == "PLANNER_RESPONSE" and str(entry.get("content") or "").strip():
                    si = entry.get("step_index")
                    if isinstance(si, int) and si > max_planner_step:
                        max_planner_step = si
    except Exception:
        return False, "", -1
    return is_main, summary or "(无标题)", max_planner_step


_META_CACHE: dict[str, dict[str, Any] | None] = {}


def fetch_conversation_meta(conv_id: str) -> dict[str, Any] | None:
    if conv_id in _META_CACHE:
        return _META_CACHE[conv_id]
    try:
        rc, out, _ = run_agentapi(["get-conversation-metadata", conv_id], timeout=8.0)
        if rc != 0 or not out:
            return None
        obj = json.loads(out)
        meta = ((obj.get("response") or {}).get("conversationMetadata") or {}).get("metadata")
        _META_CACHE[conv_id] = meta
        return meta
    except Exception:
        return None


def list_recent_main_sessions(limit: int = 10) -> list[dict[str, Any]]:
    if not BRAIN_DIR.exists():
        return []
    candidates: list[tuple[float, str, Path]] = []
    for d in BRAIN_DIR.iterdir():
        if not d.is_dir():
            continue
        tp = d / ".system_generated" / "logs" / "transcript.jsonl"
        if not tp.is_file():
            continue
        try:
            mtime = tp.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, d.name, tp))

    candidates.sort(key=lambda x: x[0], reverse=True)
    proj_names = load_project_names()

    pre_filtered: list[tuple[float, str, str, int]] = []
    for mtime, cid, tp in candidates[:60]:
        is_main, summary, max_step = extract_session_summary(tp)
        if is_main:
            pre_filtered.append((mtime, cid, summary, max_step))
        if len(pre_filtered) >= 24:
            break

    with ThreadPoolExecutor(max_workers=6) as pool:
        metas = list(pool.map(fetch_conversation_meta, [item[1] for item in pre_filtered]))

    results: list[dict[str, Any]] = []
    for (mtime, cid, summary, max_step), meta in zip(pre_filtered, metas):
        if len(results) >= limit:
            break
        if meta is not None:
            root_cid = str(meta.get("rootConversationId") or "")
            if (
                int(meta.get("nestingDepth") or 0) > 0
                or (meta.get("parentConversationId") or "")
                or (root_cid and root_cid != cid)
            ):
                continue
            pid = str(meta.get("projectId") or "outside-of-project")
        else:
            pid = "outside-of-project"
        results.append(
            {
                "id": cid,
                "project": proj_names.get(pid, pid[:12]),
                "summary": summary,
                "mtime": mtime,
                "max_step": max_step,
            }
        )
    return results


def resolve_agentapi_cmd() -> list[str]:
    """Resolve direct binary behind agentapi across Windows, macOS, and Linux."""
    default_bin = (
        Path.home() / ".gemini" / "antigravity" / "bin" / ("agentapi.bat" if os.name == "nt" else "agentapi")
    )
    candidates = [shutil.which("agentapi"), str(default_bin)]
    if os.name != "nt":
        candidates.extend(["/opt/homebrew/bin/agentapi", "/usr/local/bin/agentapi"])

    for cand in candidates:
        if not cand:
            continue
        p = Path(cand)
        if not p.exists():
            continue
        if os.name == "nt" and p.suffix.lower() == ".bat":
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r'"([^"]+language_server\.exe)"\s+agentapi', content, re.IGNORECASE)
                if m and Path(m.group(1)).exists():
                    return [m.group(1), "agentapi"]
            except Exception:
                pass
        return [str(p)]

    # macOS App bundle fallback when GUI-launched Sidecar lacks user PATH
    if sys.platform == "darwin":
        for app_ls in (
            Path("/Applications/Antigravity.app/Contents/Resources/bin/language_server"),
            Path.home() / "Applications" / "Antigravity.app" / "Contents" / "Resources" / "bin" / "language_server",
        ):
            if app_ls.exists():
                return [str(app_ls), "agentapi"]

    return [str(default_bin)]


def run_agentapi(args: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    cmd = resolve_agentapi_cmd() + args
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def extract_conversation_id(stdout: str) -> str:
    if not stdout:
        return ""
    try:
        data = json.loads(stdout)
        cid = ((data.get("payload") or {}).get("newConversation") or {}).get("conversationId")
        if isinstance(cid, str) and cid.strip():
            return cid.strip()
    except Exception:
        pass
    m = re.search(r'"conversationId"\s*:\s*"([0-9a-fA-F-]{16,})"', stdout)
    return m.group(1) if m else ""


def make_msg_dedup_key(msg: dict[str, Any]) -> str:
    mid = msg.get("message_id")
    if mid:
        return f"id:{mid}"
    seq = msg.get("seq")
    if seq:
        return f"seq:{seq}"
    raw = json.dumps(
        {
            "from": msg.get("from_user_id"),
            "ts": msg.get("create_time_ms"),
            "items": msg.get("item_list"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return "h:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def is_user_allowed(user_id: str, account: dict[str, Any]) -> bool:
    allow_from = account.get("allow_from") or ["*"]
    if isinstance(allow_from, str):
        allow_from = [x.strip() for x in allow_from.split(",") if x.strip()]
    if "*" in allow_from:
        return True
    return user_id in allow_from


def safe_send_text(client: ILinkClient, peer_id: str, text: str, context_token: str) -> bool:
    wait_sec = 0.0
    with locked_state() as state:
        now = time.time()
        if now < float(state.get("throttle_until") or 0.0):
            log("Skip send: currently in iLink throttle cooldown.")
            return False
        last_ts = float(state.get("last_send_ts") or 0.0)
        if now - last_ts < 1.5:
            wait_sec = 1.5 - (now - last_ts)
        state["last_send_ts"] = now + wait_sec
    if wait_sec > 0:
        time.sleep(wait_sec)
    try:
        client.send_text(peer_id, text, context_token)
        with locked_state() as state:
            state["last_send_ts"] = time.time()
        return True
    except ILinkThrottledError as exc:
        log(f"THROTTLED by iLink: {exc}")
        with locked_state() as state:
            state["throttle_until"] = time.time() + THROTTLE_COOLDOWN_SEC
        return False
    except Exception as exc:
        log(f"send_text error: {exc}")
        return False


def safe_send_media(
    client: ILinkClient, peer_id: str, path: Path, context_token: str, kind: str = "file"
) -> bool:
    wait_sec = 0.0
    with locked_state() as state:
        now = time.time()
        if now < float(state.get("throttle_until") or 0.0):
            log("Skip media send: currently in iLink throttle cooldown.")
            return False
        last_ts = float(state.get("last_send_ts") or 0.0)
        if now - last_ts < 1.5:
            wait_sec = 1.5 - (now - last_ts)
        state["last_send_ts"] = now + wait_sec
    if wait_sec > 0:
        time.sleep(wait_sec)
    try:
        if kind == "image":
            client.send_image_file(peer_id, context_token, path)
        else:
            client.send_attachment_file(peer_id, context_token, path)
        with locked_state() as state:
            state["last_send_ts"] = time.time()
        return True
    except ILinkThrottledError as exc:
        log(f"THROTTLED by iLink during media send: {exc}")
        with locked_state() as state:
            state["throttle_until"] = time.time() + THROTTLE_COOLDOWN_SEC
        return False
    except Exception as exc:
        log(f"send_{kind} error: {exc}")
        return False


class WeChatBridge:
    def __init__(self) -> None:
        self.client: ILinkClient | None = None
        self.account: dict[str, Any] | None = None
        self.account_mtime: float = 0.0
        self.stop_event = threading.Event()

        self._batch_lock = threading.Lock()
        self._pending_batches: dict[str, dict[str, Any]] = {}
        self._batch_timers: dict[str, threading.Timer] = {}

    def _reload_account_if_needed(self) -> bool:
        if not ACCOUNT_FILE.exists():
            return False
        mtime = ACCOUNT_FILE.stat().st_mtime
        if self.account is not None and mtime == self.account_mtime:
            return True
        acc = load_account()
        if not acc:
            return False
        self.account = acc
        self.account_mtime = mtime
        self.client = ILinkClient(
            token=acc.get("bot_token", ""),
            base_url=acc.get("base_url") or DEFAULT_BASE_URL,
            cdn_base_url=acc.get("cdn_base_url") or DEFAULT_CDN_BASE_URL,
            route_tag=acc.get("route_tag", ""),
            use_system_proxy=bool(acc.get("use_system_proxy", False)),
        )
        log(
            f"Loaded WeChat account bot_id={acc.get('ilink_bot_id', '')} "
            f"allow_from={acc.get('allow_from')}"
        )
        return True

    def _start_typing(self, peer_id: str, context_token: str) -> str:
        if not self.client:
            return ""
        ticket = ""
        try:
            ticket = self.client.get_config(peer_id, context_token)
            if ticket:
                self.client.send_typing(peer_id, ticket, TYPING_START)
        except Exception as exc:
            log(f"typing start ignored: {exc}")
        return ticket

    def _typing_heartbeat_loop(self) -> None:
        while not self.stop_event.wait(TYPING_HEARTBEAT_INTERVAL):
            if not self.client:
                continue
            now = time.time()
            to_refresh: list[tuple[str, str]] = []
            expired_cids: list[str] = []
            state = load_state()
            tracked = state.get("tracked_conversations") or {}
            peers = state.get("peers") or {}
            for cid, info in tracked.items():
                if not info.get("typing_active"):
                    continue
                started = float(info.get("typing_started_at") or 0.0)
                if now - started > TYPING_MAX_DURATION:
                    expired_cids.append(cid)
                    continue
                peer_id = info.get("peer_id") or ""
                ticket = (peers.get(peer_id) or {}).get("typing_ticket") or ""
                if peer_id and ticket:
                    to_refresh.append((peer_id, ticket))

            if expired_cids:
                with locked_state() as st:
                    for cid in expired_cids:
                        if cid in st["tracked_conversations"]:
                            st["tracked_conversations"][cid]["typing_active"] = False

            for peer_id, ticket in to_refresh:
                try:
                    self.client.send_typing(peer_id, ticket, TYPING_START)
                except Exception:
                    pass

    def _handle_slash_command(self, peer_id: str, context_token: str, text: str) -> bool:
        stripped = text.strip()
        if not stripped.startswith("/"):
            return False
        parts = stripped.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/new", "/reset", "/unbind"):
            with locked_state() as state:
                old_cid = state.get("active_conversation_id") or ""
                state["active_conversation_id"] = ""
            if cmd == "/unbind":
                msg = "已解除当前会话绑定，下一条消息将在 WeChat Assistant 中开启全新独立会话。"
            else:
                msg = "已重置对话上下文，下一条消息将在 Antigravity 中开启全新小助理会话。"
            if old_cid:
                msg += f"\n(上一会话 ID: {old_cid[:8]}...)"
            assert self.client is not None
            safe_send_text(self.client, peer_id, msg, context_token)
            return True

        if cmd in ("/ls", "/list", "/sessions"):
            sessions = list_recent_main_sessions(limit=10)
            if not sessions:
                reply = "未在本地找到活跃的 Antigravity 主会话。"
            else:
                with locked_state() as state:
                    cur_cid = (state.get("active_conversation_id") or "").strip()
                    state["last_listed_sessions"] = [s["id"] for s in sessions]
                lines = ["最近活跃的 Antigravity 会话（发送 /bind <序号或ID> 接管）："]
                for i, s in enumerate(sessions, start=1):
                    mark = "★ " if s["id"] == cur_cid else ""
                    tstr = datetime.fromtimestamp(s["mtime"]).strftime("%m-%d %H:%M")
                    lines.append(
                        f"{i}. {mark}[{s['project']}] {s['summary']}\n"
                        f"   ID: {s['id'][:8]} · {tstr}"
                    )
                lines.append("\n提示：发 /bind 2 接管第 2 个会话；发 /unbind 切回独立小助理。")
                reply = "\n".join(lines)
            assert self.client is not None
            safe_send_text(self.client, peer_id, reply, context_token)
            return True

        if cmd == "/bind":
            assert self.client is not None
            if not arg:
                reply = (
                    "用法：/bind <序号 | ID前缀 | 完整UUID>\n"
                    "1. 先发 /ls 查看最近会话编号\n"
                    "2. 再发 /bind 2 绑定对应会话（或直接 /bind cb250db2）\n"
                    "3. 发 /unbind 或 /new 解除绑定"
                )
                safe_send_text(self.client, peer_id, reply, context_token)
                return True

            target_cid = ""
            if arg.isdigit():
                idx = int(arg)
                state = load_state()
                listed: list[str] = state.get("last_listed_sessions") or []
                if not listed:
                    sessions = list_recent_main_sessions(limit=10)
                    listed = [s["id"] for s in sessions]
                    with locked_state() as st:
                        st["last_listed_sessions"] = listed
                if 1 <= idx <= len(listed):
                    target_cid = listed[idx - 1]
                else:
                    safe_send_text(
                        self.client,
                        peer_id,
                        f"序号 {idx} 超出范围（当前列表共 {len(listed)} 条），请先发 /ls 刷新列表。",
                        context_token,
                    )
                    return True
            else:
                needle = arg.strip().lower()
                if not _UUID_RE.match(needle):
                    safe_send_text(
                        self.client,
                        peer_id,
                        "无效的会话序号或 ID 格式。请先发 /ls 查看编号，或输入至少 6 位会话 ID 前缀。",
                        context_token,
                    )
                    return True
                matches = [
                    d.name
                    for d in BRAIN_DIR.iterdir()
                    if d.is_dir() and d.name.lower().startswith(needle)
                ] if BRAIN_DIR.exists() else []
                if len(matches) == 1:
                    target_cid = matches[0]
                elif len(matches) > 1:
                    safe_send_text(
                        self.client,
                        peer_id,
                        f"前缀 `{arg}` 匹配到 {len(matches)} 个会话，请多输入几位字符，或先发 /ls 按序号绑定。",
                        context_token,
                    )
                    return True
                else:
                    safe_send_text(
                        self.client,
                        peer_id,
                        f"未找到以 `{arg}` 开头的本地会话，请发 /ls 确认。",
                        context_token,
                    )
                    return True

            tp = BRAIN_DIR / target_cid / ".system_generated" / "logs" / "transcript.jsonl"
            is_main, summary, max_step = extract_session_summary(tp) if tp.exists() else (True, "(新会话)", -1)
            meta = fetch_conversation_meta(target_cid)
            root_cid = str((meta or {}).get("rootConversationId") or "")
            if meta is not None and (
                int(meta.get("nestingDepth") or 0) > 0
                or (meta.get("parentConversationId") or "")
                or (root_cid and root_cid != target_cid)
            ):
                safe_send_text(
                    self.client,
                    peer_id,
                    f"会话 {target_cid[:8]} 是一个子代理/工作流子会话，不支持直接绑定。请发 /ls 选择主会话。",
                    context_token,
                )
                return True

            proj_names = load_project_names()
            pid = str((meta or {}).get("projectId") or "outside-of-project")
            proj_label = proj_names.get(pid, pid[:12])
            now_iso = datetime.now(timezone.utc).isoformat()

            with locked_state() as state:
                state["active_conversation_id"] = target_cid
                tr = state["tracked_conversations"].setdefault(
                    target_cid,
                    {
                        "peer_id": peer_id,
                        "last_sent_step_index": max_step,
                        "pending_from_wechat": False,
                        "typing_active": False,
                        "created_at": now_iso,
                    },
                )
                tr["peer_id"] = peer_id
                tr["last_sent_step_index"] = max(int(tr.get("last_sent_step_index", -1)), max_step)
                tr["is_external_bind"] = True
                tr["updated_at"] = now_iso

            reply = (
                f"已绑定到桌面端会话：\n"
                f"- 项目: {proj_label}\n"
                f"- 摘要: {summary}\n"
                f"- ID: {target_cid}\n"
                "现在直接发消息即可遥控该工作区会话；想切回独立小助理请发 /unbind 或 /new。"
            )
            safe_send_text(self.client, peer_id, reply, context_token)
            return True

        if cmd in ("/projects", "/proj", "/p"):
            assert self.client is not None
            projects = list_user_projects()
            state = load_state()
            cur_pid = (state.get("target_project_id") or "").strip()
            with locked_state() as st:
                st["last_listed_projects"] = [p["id"] for p in projects]
            if not projects:
                reply = "未找到本地 Antigravity 项目。"
            else:
                lines = ["可用 Antigravity 项目（发 /new-in <序号或名称> [首条指令]）："]
                for i, p in enumerate(projects, start=1):
                    mark = "★ " if p["id"] == cur_pid else ""
                    folder_hint = p["folders"][0] if p["folders"] else "无挂载目录"
                    lines.append(f"{i}. {mark}{p['name']} ({folder_hint})")
                lines.append("\n示例：/new-in 2 帮我看看最新进展  |  /new-in default 切回独立小助理")
                reply = "\n".join(lines)
            safe_send_text(self.client, peer_id, reply, context_token)
            return True

        if cmd in ("/new-in", "/in"):
            assert self.client is not None
            if not arg:
                reply = (
                    "用法：/new-in <项目序号或名称> [可选：首条指令]\n"
                    "1. 先发 /projects 查看所有项目编号\n"
                    "2. 例：/new-in 2（切换后下条消息在该项目开新对话）\n"
                    "3. 例：/new-in MyProject 帮我看看最新进展（一步到位）\n"
                    "4. 发 /new-in default 切回默认项目"
                )
                safe_send_text(self.client, peer_id, reply, context_token)
                return True

            state = load_state()
            listed_pids: list[str] = state.get("last_listed_projects") or []
            # First try matching the entire arg (handles project names with spaces), else first token + prompt
            target_proj, cands = resolve_project(arg, listed_pids)
            initial_prompt = ""
            if target_proj is None and " " in arg:
                first_tok, rest = arg.split(None, 1)
                tp2, cands2 = resolve_project(first_tok, listed_pids)
                if tp2 is not None:
                    target_proj = tp2
                    initial_prompt = rest.strip()
                elif cands2 and not cands:
                    cands = cands2

            if target_proj is None:
                if cands:
                    names = "\n".join(f"- {p['name']}" for p in cands[:8])
                    reply = f"匹配到多个项目，请写更具体或先发 /projects 按序号指定：\n{names}"
                else:
                    reply = f"未找到项目 `{arg}`，请先发 /projects 查看可用项目编号。"
                safe_send_text(self.client, peer_id, reply, context_token)
                return True

            try:
                switch_sidecar_project_in_config(target_proj)
            except Exception as exc:
                safe_send_text(self.client, peer_id, f"切换项目配置失败：{exc}", context_token)
                return True

            with locked_state() as st:
                st["active_conversation_id"] = ""
                st["target_project_id"] = target_proj["id"]
                st["target_project_name"] = target_proj["name"]
                if initial_prompt:
                    st["pending_startup_dispatch"] = {
                        "peer_id": peer_id,
                        "context_token": context_token,
                        "text": initial_prompt,
                    }
                else:
                    st["pending_startup_dispatch"] = None

            folder_str = ", ".join(target_proj.get("folders") or []) or "无挂载目录"
            if initial_prompt:
                reply = (
                    f"已切换到项目 [{target_proj['name']}]（{folder_str}），"
                    "正在热重载并为你拉起新会话，请稍候..."
                )
            else:
                reply = (
                    f"已将新会话默认项目切换为：[{target_proj['name']}]\n"
                    f"- 目录: {folder_str}\n"
                    "正在 1 秒内热重载项目上下文，接下来直接发消息即可在该项目下创建新对话。\n"
                    "（发 /new-in default 可随时切回 WeChat Assistant）"
                )
            safe_send_text(self.client, peer_id, reply, context_token)
            log(f"Switching sidecar projectId to {target_proj['id']} ({target_proj['name']}); triggering clean respawn.")
            threading.Timer(0.6, os._exit, args=(0,)).start()
            return True

        if cmd == "/watch":
            assert self.client is not None
            if not arg or arg.lower() == "list":
                state = load_state()
                watched = state.get("watched_conversations") or {}
                if not watched:
                    reply = (
                        "当前没有正在盯梢的任务。\n"
                        "用法：先发 /ls 查看桌面会话序号，再发 /watch <序号或ID前缀>；"
                        "该会话跑完（或出错）时会自动把结果推到你微信！"
                    )
                else:
                    lines = ["当前正在盯梢的任务（跑完自动通知你）："]
                    for i, (wcid, winfo) in enumerate(watched.items(), start=1):
                        lines.append(
                            f"{i}. [{winfo.get('project', '?')}] {winfo.get('summary', '')} ({wcid[:8]})"
                        )
                    lines.append("\n发 /unwatch <ID前缀或all> 可取消盯梢。")
                    reply = "\n".join(lines)
                safe_send_text(self.client, peer_id, reply, context_token)
                return True

            if arg.lower() == "clear":
                with locked_state() as st:
                    cnt = len(st.get("watched_conversations") or {})
                    st["watched_conversations"] = {}
                safe_send_text(self.client, peer_id, f"已清空全部 {cnt} 条任务盯梢。", context_token)
                return True

            target_cid = ""
            if arg.isdigit():
                idx = int(arg)
                state = load_state()
                listed: list[str] = state.get("last_listed_sessions") or []
                if not listed:
                    sessions = list_recent_main_sessions(limit=10)
                    listed = [s["id"] for s in sessions]
                    with locked_state() as st:
                        st["last_listed_sessions"] = listed
                if 1 <= idx <= len(listed):
                    target_cid = listed[idx - 1]
                else:
                    safe_send_text(
                        self.client,
                        peer_id,
                        f"序号 {idx} 超出范围（当前共 {len(listed)} 条），请先发 /ls 刷新。",
                        context_token,
                    )
                    return True
            else:
                needle = arg.strip().lower()
                matches = [
                    d.name
                    for d in BRAIN_DIR.iterdir()
                    if d.is_dir() and d.name.lower().startswith(needle)
                ] if BRAIN_DIR.exists() and _UUID_RE.match(needle) else []
                if len(matches) == 1:
                    target_cid = matches[0]
                elif len(matches) > 1:
                    safe_send_text(self.client, peer_id, f"前缀 `{arg}` 匹配多个会话，请多输几位或发 /ls 按序号。", context_token)
                    return True
                else:
                    safe_send_text(self.client, peer_id, f"未找到会话 `{arg}`，请先发 /ls 确认。", context_token)
                    return True

            tp = BRAIN_DIR / target_cid / ".system_generated" / "logs" / "transcript.jsonl"
            _, summary, max_step = extract_session_summary(tp) if tp.exists() else (True, "(新会话)", -1)
            meta = fetch_conversation_meta(target_cid)
            proj_names = load_project_names()
            pid = str((meta or {}).get("projectId") or "outside-of-project")
            proj_label = proj_names.get(pid, pid[:12])

            with locked_state() as st:
                st.setdefault("watched_conversations", {})[target_cid] = {
                    "peer_id": peer_id,
                    "last_sent_step_index": max_step,
                    "project": proj_label,
                    "summary": summary,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            reply = (
                f"👀 已开启长任务盯梢：\n"
                f"- 项目: {proj_label}\n"
                f"- 会话: {summary} ({target_cid[:8]})\n"
                "该会话跑完（或报错）后会自动把结果推送到你微信！发 /unwatch 可取消。"
            )
            safe_send_text(self.client, peer_id, reply, context_token)
            return True

        if cmd == "/unwatch":
            assert self.client is not None
            with locked_state() as st:
                watched: dict[str, Any] = st.setdefault("watched_conversations", {})
                if not arg or arg.lower() == "all":
                    cnt = len(watched)
                    st["watched_conversations"] = {}
                    reply = f"已取消全部 {cnt} 条任务盯梢。"
                else:
                    needle = arg.strip().lower()
                    keys = list(watched.keys())
                    removed = ""
                    if needle.isdigit() and 1 <= int(needle) <= len(keys):
                        removed = keys[int(needle) - 1]
                    else:
                        hits = [k for k in keys if k.lower().startswith(needle)]
                        if len(hits) == 1:
                            removed = hits[0]
                    if removed:
                        watched.pop(removed, None)
                        reply = f"已取消对会话 {removed[:8]} 的盯梢。"
                    else:
                        reply = f"未在盯梢列表中找到 `{arg}`，发 /watch 查看当前列表。"
            safe_send_text(self.client, peer_id, reply, context_token)
            return True

        if cmd in ("/screen", "/screenshot"):
            assert self.client is not None
            shot_path = DATA_DIR / "screen.jpg"
            ok, detail = capture_desktop_screenshot(shot_path)
            if ok:
                sent = safe_send_media(self.client, peer_id, shot_path, context_token, kind="image")
                if not sent:
                    safe_send_text(self.client, peer_id, "截图已生成但回传微信失败（可能处于限频冷却期）。", context_token)
            elif detail == "locked":
                sys_info = collect_system_status()
                reply = (
                    "🔒 当前电脑处于锁屏状态，Windows 安全机制禁止截取锁屏画面。\n"
                    "不过后台程序照常运行，附上当前状态：\n\n" + sys_info
                )
                safe_send_text(self.client, peer_id, reply, context_token)
            else:
                sys_info = collect_system_status()
                reply = f"截屏失败（{detail}），附上当前系统状态：\n\n{sys_info}"
                safe_send_text(self.client, peer_id, reply, context_token)
            return True

        if cmd == "/sys":
            assert self.client is not None
            safe_send_text(self.client, peer_id, collect_system_status(), context_token)
            return True

        if cmd == "/digest":
            assert self.client is not None
            sub = arg.strip().lower()
            if sub in ("auto on", "on"):
                with locked_state() as st:
                    st["digest_auto_push"] = True
                safe_send_text(
                    self.client,
                    peer_id,
                    "✅ 已开启每日精选日报自动推送微信（每天 8:00 & 18:00 随邮件同步推送速览）。",
                    context_token,
                )
                return True
            if sub in ("auto off", "off"):
                with locked_state() as st:
                    st["digest_auto_push"] = False
                safe_send_text(
                    self.client,
                    peer_id,
                    "已关闭每日精选日报自动推送微信（发 /digest 仍可随时手动查看）。",
                    context_token,
                )
                return True

            latest = find_latest_digest()
            if latest is None:
                safe_send_text(self.client, peer_id, "暂未找到每日简报文件（daily-digest 目录为空）。", context_token)
                return True

            if sub == "file":
                ok = safe_send_media(self.client, peer_id, latest, context_token, kind="file")
                if not ok:
                    safe_send_text(self.client, peer_id, f"发送文件 {latest.name} 失败，请稍后重试。", context_token)
                return True

            md_text = latest.read_text(encoding="utf-8", errors="replace")
            summary_msg = format_digest_for_wechat(md_text, filename=latest.name)
            safe_send_text(self.client, peer_id, summary_msg, context_token)
            return True

        if cmd == "/status":
            state = load_state()
            cid = state.get("active_conversation_id") or ""
            cid_display = cid or "（尚未创建，发消息自动创建）"
            if cid:
                tr = (state.get("tracked_conversations") or {}).get(cid) or {}
                meta = fetch_conversation_meta(cid)
                proj_names = load_project_names()
                pid = str((meta or {}).get("projectId") or "")
                proj_label = proj_names.get(pid, pid[:12]) if pid else ""
                tag = " [外部绑定]" if tr.get("is_external_bind") else ""
                if proj_label:
                    cid_display = f"{cid[:8]}... ({proj_label}){tag}"
            target_proj_name = state.get("target_project_name") or "WeChat Assistant"
            model = state.get("model") or "（跟随 Antigravity 默认）"
            inbox = state.get("inbox_dir") or str(DEFAULT_INBOX_DIR)
            watch_n = len(state.get("watched_conversations") or {})
            digest_auto = "开启" if state.get("digest_auto_push", True) else "关闭"
            reply = (
                "Antigravity 微信小助理运行正常\n"
                f"- 当前对话: {cid_display}\n"
                f"- 新建默认项目: {target_proj_name}\n"
                f"- 指定模型: {model}\n"
                f"- 盯梢中任务: {watch_n} 个\n"
                f"- 每日简报自动推送: {digest_auto}\n"
                f"- 文件收件箱: {inbox}\n"
                "发 /help 查看完整操作指南"
            )
            assert self.client is not None
            safe_send_text(self.client, peer_id, reply, context_token)
            return True

        if cmd == "/model":
            if not arg:
                state = load_state()
                cur = state.get("model") or "default"
                reply = (
                    f"当前指定模型: {cur}\n"
                    "用法: /model flash | /model pro | /model gemini-4-carbon-medium | /model default\n"
                    "（将在下次 /new 或 /new-in 新建会话时生效）"
                )
            else:
                new_model = "" if arg.lower() in ("default", "none", "reset") else arg
                with locked_state() as state:
                    state["model"] = new_model
                reply = f"已设置新会话模型为: {new_model or '默认'}（发送 /new 后生效）"
            assert self.client is not None
            safe_send_text(self.client, peer_id, reply, context_token)
            return True

        if cmd == "/help":
            state = load_state()
            inbox_display = state.get("inbox_dir") or str(DEFAULT_INBOX_DIR)
            reply = (
                "🐾 Antigravity 微信小助理 · 操作速查\n\n"
                "【一、日常转发 & 聊天】\n"
                f"• 直接发文字、语音（原生多模态听原声）、截图、PDF/Word/代码文件（自动存 {inbox_display}）\n"
                "• 连续转发多张图/文件+说明文字，1.8秒内自动合并成同一轮\n"
                "• 长按历史回复点「引用」可直接带上下文追问\n"
                "• 说“把文件/图发回微信”可反向传回手机\n\n"
                "【二、在指定项目下开新会话】\n"
                "• /projects — 查看所有项目编号与目录\n"
                "• /new-in 2 — 切换新会话到 2 号项目\n"
                "• /new-in <项目名> <首条指令> — 切项目并直接发首条指令\n"
                "• /new-in default — 切回默认项目\n\n"
                "【三、接管电脑端已有会话】\n"
                "• /ls — 查看最近活跃的桌面主会话编号\n"
                "• /bind 2 — 接管 2 号会话（也支持填 ID 前缀）\n"
                "• /unbind — 解除接管，下条消息重开新会话\n\n"
                "【四、长任务盯梢（跑完微信喊我）】\n"
                "• /watch 2 — 盯住 /ls 里的 2 号会话，跑完/报错自动推结果\n"
                "• /watch — 查看当前盯梢列表\n"
                "• /unwatch all — 取消全部盯梢（也可填序号/ID）\n\n"
                "【五、电脑巡检 & 每日简报】\n"
                "• /screen — 远程截一张电脑桌面图（锁屏时自动改发系统状态）\n"
                "• /sys — 查看 CPU/内存/磁盘/活跃窗口\n"
                "• /digest — 获取最新每日简报（如已配置）\n\n"
                "【六、通用控制】\n"
                "• /new — 在当前目标项目下开全新对话\n"
                "• /model <模型名|default> — 设置新会话模型\n"
                "• /status — 查看当前会话、项目、盯梢与收件箱状态"
            )
            assert self.client is not None
            safe_send_text(self.client, peer_id, reply, context_token)
            return True

        return False

    def _build_prompt(
        self, texts: list[str], attachments: list[dict[str, str]], warnings: list[str]
    ) -> str:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"[微信小助理消息 · {now_str}]"]
        combined_text = "\n".join(t for t in texts if t.strip()).strip()
        if combined_text:
            lines.append(combined_text)
        has_audio = any(att.get("kind") == "audio" for att in attachments)
        has_non_audio = any(att.get("kind") != "audio" for att in attachments)
        if attachments:
            lines.append("")
            if has_audio and not has_non_audio:
                lines.append("主人发来以下语音消息（已自动解码为可听音频，请直接调用 read_file 听取原声内容与语气后自然回复）：")
            elif has_audio:
                lines.append("已自动下载并保存以下微信附件到本地收件箱（语音请优先调用 read_file 直接听取原声，其余附件也请调用 read_file 查看）：")
            else:
                lines.append("已自动下载并保存以下微信附件到本地收件箱（请直接调用 read_file 查看内容）：")
            for att in attachments:
                lines.append(f"- [{att['kind']}] {att['name']} -> {att['path']}")
        if warnings:
            lines.append("")
            lines.append("附件处理提示：")
            for w in warnings:
                lines.append(f"- {w}")
        if not combined_text and has_non_audio:
            lines.append("")
            lines.append("主人仅转发了上述附件，请先查看其核心内容，给出简明摘要并询问接下来需要怎么处理（如总结、翻译、存 Notion 或归档）。")
        lines.append("")
        lines.append(
            f"（提醒：你的最终文本回复会自动推送回主人的微信，请适合手机阅读；"
            f"如需回传本地文件或图片到微信，请优先调用 MCP 工具 `wechat_send_file` / `wechat_send_image`，"
            f"或运行 `python {_SEND_REPLY_SCRIPT} --file <路径>` / `--image <路径>`。）"
        )
        return "\n".join(lines)

    def _dispatch_batch(self, peer_id: str) -> None:
        with self._batch_lock:
            batch = self._pending_batches.pop(peer_id, None)
            self._batch_timers.pop(peer_id, None)
        if not batch:
            return

        texts: list[str] = batch["texts"]
        attachments: list[dict[str, str]] = batch["attachments"]
        warnings: list[str] = batch["warnings"]
        context_token: str = batch["context_token"]

        if not texts and not attachments and not warnings:
            return

        prompt = self._build_prompt(texts, attachments, warnings)
        ticket = self._start_typing(peer_id, context_token)

        with locked_state() as state:
            peer_info = state["peers"].setdefault(peer_id, {})
            peer_info["context_token"] = context_token
            if ticket:
                peer_info["typing_ticket"] = ticket
            peer_info["updated_at"] = datetime.now(timezone.utc).isoformat()
            active_cid = (state.get("active_conversation_id") or "").strip()
            model_opt = (state.get("model") or "").strip()

        now_iso = datetime.now(timezone.utc).isoformat()
        used_cid = ""

        # Try continuing active conversation first via `agentapi send-message`
        if active_cid:
            # Mark pending BEFORE calling agentapi to eliminate Stop-hook race.
            with locked_state() as state:
                tr = state["tracked_conversations"].setdefault(
                    active_cid,
                    {
                        "peer_id": peer_id,
                        "last_sent_step_index": -1,
                        "created_at": now_iso,
                    },
                )
                tr["peer_id"] = peer_id
                tr["pending_from_wechat"] = True
                tr["typing_active"] = True
                tr["typing_started_at"] = time.time()
                tr["updated_at"] = now_iso

            rc, out, err = run_agentapi(["send-message", "--", active_cid, prompt])
            if rc == 0:
                used_cid = active_cid
                log(f"Sent message to existing conversation {active_cid}")
            else:
                log(f"send-message failed (rc={rc}, err={err[:200]})")
                was_external = False
                with locked_state() as state:
                    tr_old = state["tracked_conversations"].get(active_cid)
                    if tr_old:
                        was_external = bool(tr_old.get("is_external_bind"))
                        tr_old["pending_from_wechat"] = False
                        tr_old["typing_active"] = False
                    if not was_external:
                        state["active_conversation_id"] = ""
                if was_external:
                    if self.client:
                        if ticket:
                            try:
                                self.client.send_typing(peer_id, ticket, TYPING_STOP)
                            except Exception:
                                pass
                        safe_send_text(
                            self.client,
                            peer_id,
                            f"向已绑定的桌面会话 {active_cid[:8]} 发送消息失败（rc={rc}）：{err[:240] or out[:240]}\n"
                            "请发 /ls 重新绑定，或发 /unbind 切回独立小助理。",
                            context_token,
                        )
                    return

        if not used_cid:
            title = f"微信小助理 {datetime.now().strftime('%m-%d %H:%M')}"
            cmd_args = ["new-conversation", f"--title={title}"]
            if model_opt:
                cmd_args.append(f"--model={model_opt}")
            cmd_args.extend(["--", prompt])
            rc, out, err = run_agentapi(cmd_args)
            if rc != 0:
                log(f"agentapi new-conversation failed rc={rc} stdout={out[:200]} stderr={err[:300]}")
                if self.client:
                    if ticket:
                        try:
                            self.client.send_typing(peer_id, ticket, TYPING_STOP)
                        except Exception:
                            pass
                    safe_send_text(
                        self.client,
                        peer_id,
                        f"无法唤醒本地 Antigravity（agentapi 退出码 {rc}）：{err[:300] or out[:300]}",
                        context_token,
                    )
                return

            new_cid = extract_conversation_id(out)
            log(f"Created new conversation {new_cid or '(unknown)'}")
            if new_cid:
                with locked_state() as state:
                    state["active_conversation_id"] = new_cid
                    state["tracked_conversations"][new_cid] = {
                        "peer_id": peer_id,
                        "last_sent_step_index": -1,
                        "pending_from_wechat": True,
                        "typing_active": True,
                        "typing_started_at": time.time(),
                        "created_at": now_iso,
                        "updated_at": now_iso,
                    }

    def _enqueue_message(self, msg: dict[str, Any]) -> None:
        if not self.client or not self.account:
            return
        if int(msg.get("message_type") or 0) != MSG_TYPE_USER:
            return
        peer_id = (msg.get("from_user_id") or "").strip()
        context_token = (msg.get("context_token") or "").strip()
        if not peer_id:
            return
        if not is_user_allowed(peer_id, self.account):
            log(f"Ignored message from unauthorized peer: {peer_id}")
            return

        dedup_key = make_msg_dedup_key(msg)
        with locked_state() as state:
            processed: list[str] = state.get("processed_msg_ids") or []
            if dedup_key in processed:
                return
            processed.append(dedup_key)
            if len(processed) > MAX_PROCESSED_IDS:
                processed = processed[-MAX_PROCESSED_IDS:]
            state["processed_msg_ids"] = processed
            if context_token:
                peer_info = state["peers"].setdefault(peer_id, {})
                peer_info["context_token"] = context_token
                peer_info["updated_at"] = datetime.now(timezone.utc).isoformat()
            inbox_dir = Path(state.get("inbox_dir") or DEFAULT_INBOX_DIR)

        text, attachments, warnings = self.client.parse_and_download_inbound(msg, inbox_dir)
        log(
            f"Inbound from {peer_id}: text_len={len(text)} "
            f"attachments={len(attachments)} warnings={len(warnings)}"
        )

        # Slash commands execute immediately without debounce.
        if text and not attachments and self._handle_slash_command(peer_id, context_token, text):
            return

        if not text and not attachments and not warnings:
            # Unsupported rich card (e.g.,公众号卡片/合并转发) arrived empty
            safe_send_text(
                self.client,
                peer_id,
                "收到一条微信暂未在 iLink 开放解析的富媒体卡片（如公众号卡片或合并转发）。\n"
                "你可以：\n"
                "1) 点文章右上角「复制链接」发给我；\n"
                "2) 或点「收藏」后跟我说“总结刚收藏的文章”，我直接通过 weflow 读取！",
                context_token,
            )
            return

        with self._batch_lock:
            existing = self._pending_batches.get(peer_id)
            if existing is None:
                existing = {
                    "texts": [],
                    "attachments": [],
                    "warnings": [],
                    "context_token": context_token,
                }
                self._pending_batches[peer_id] = existing
            if text:
                existing["texts"].append(text)
            existing["attachments"].extend(attachments)
            existing["warnings"].extend(warnings)
            if context_token:
                existing["context_token"] = context_token

            old_timer = self._batch_timers.get(peer_id)
            if old_timer is not None:
                old_timer.cancel()
            timer = threading.Timer(DEBOUNCE_SECONDS, self._dispatch_batch, args=(peer_id,))
            timer.daemon = True
            self._batch_timers[peer_id] = timer
            timer.start()

    def run_forever(self) -> None:
        log(f"Starting bridge.py (pid={os.getpid()}), agentapi={resolve_agentapi_cmd()}")
        hb = threading.Thread(target=self._typing_heartbeat_loop, daemon=True)
        hb.start()

        waiting_logged = False
        session_expired_until = 0.0

        while not self.stop_event.is_set():
            if not self._reload_account_if_needed():
                if not waiting_logged:
                    log(
                        f"Waiting for WeChat login credentials at {ACCOUNT_FILE} "
                        "(run `/wechat-setup` in Antigravity or `python setup.py --all` to bind QR)..."
                    )
                    waiting_logged = True
                time.sleep(3.0)
                continue
            waiting_logged = False

            # Process one-shot deferred dispatch left by `/new-in <project> <prompt>` before respawn.
            pending_start: dict[str, Any] | None = None
            with locked_state() as st:
                if st.get("pending_startup_dispatch"):
                    pending_start = st["pending_startup_dispatch"]
                    st["pending_startup_dispatch"] = None
            if pending_start:
                try:
                    p_peer = str(pending_start.get("peer_id") or "")
                    p_tok = str(pending_start.get("context_token") or "")
                    p_txt = str(pending_start.get("text") or "")
                    if p_peer and p_txt:
                        log(f"Running deferred /new-in prompt for {p_peer}")
                        with self._batch_lock:
                            self._pending_batches[p_peer] = {
                                "texts": [p_txt],
                                "attachments": [],
                                "warnings": [],
                                "context_token": p_tok,
                            }
                        self._dispatch_batch(p_peer)
                except Exception as exc:
                    log(f"Deferred startup dispatch error: {exc}")

            if time.time() < session_expired_until:
                # Re-check if user refreshed account.json via login.py
                old_mtime = self.account_mtime
                self._reload_account_if_needed()
                if self.account_mtime != old_mtime:
                    log("Detected refreshed account.json, resuming polling!")
                    session_expired_until = 0.0
                else:
                    time.sleep(5.0)
                    continue

            state = load_state()
            buf = state.get("get_updates_buf") or ""
            assert self.client is not None
            try:
                resp = self.client.get_updates(buf, timeout_ms=35000)
                new_buf = resp.get("get_updates_buf")
                if isinstance(new_buf, str) and new_buf and new_buf != buf:
                    with locked_state() as st:
                        st["get_updates_buf"] = new_buf
                msgs = resp.get("msgs") or []
                for msg in msgs:
                    try:
                        self._enqueue_message(msg)
                    except Exception as exc:
                        log(f"Error handling inbound message: {exc}")
            except ILinkSessionExpiredError as exc:
                log(f"SESSION EXPIRED ({exc}). Pausing poll for 15 min; run login.py to re-bind.")
                session_expired_until = time.time() + 900.0
            except ILinkError as exc:
                log(f"iLink API error: {exc}; retrying in 5s")
                time.sleep(5.0)
            except Exception as exc:
                log(f"Unexpected poll error: {exc}; retrying in 5s")
                time.sleep(5.0)


def main() -> int:
    while True:
        try:
            with file_lock(BRIDGE_LOCK_FILE, timeout_sec=1.0):
                bridge = WeChatBridge()
                bridge.run_forever()
                return 0
        except TimeoutError:
            log("Another wechat-assistant bridge instance holds the lock; sleeping 15s...")
            time.sleep(15.0)
        except KeyboardInterrupt:
            log("Shutting down on KeyboardInterrupt.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
