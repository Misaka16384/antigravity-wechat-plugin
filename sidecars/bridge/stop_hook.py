"""Antigravity Stop Hook: forwards replies for WeChat sessions and one-shot /watch alerts to WeChat."""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config_paths import (
    DEFAULT_BASE_URL,
    DEFAULT_CDN_BASE_URL,
    HOOK_LOG_FILE,
    load_account,
    load_state,
    locked_state,
)
from ilink_client import ILinkClient, ILinkThrottledError, TYPING_STOP

THROTTLE_COOLDOWN_SEC = 1800.0
_FILE_LINK_RE = re.compile(r"\[([^\]]+)\]\(file:///[^)]+\)")
_HTTP_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def hook_log(msg: str) -> None:
    try:
        HOOK_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(HOOK_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} [stop_hook] {msg}\n")
    except Exception:
        pass


def format_for_wechat(text: str) -> str:
    text = _FILE_LINK_RE.sub(r"\1", text)

    def _replace_http(m: re.Match[str]) -> str:
        label, url = m.group(1).strip(), m.group(2).strip()
        if label == url or url.startswith(label):
            return url
        return f"{label} ({url})"

    text = _HTTP_LINK_RE.sub(_replace_http, text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_latest_reply(transcript_path: str, after_step_index: int) -> tuple[int, str]:
    """Read transcript_full.jsonl (preferred) or transcript.jsonl and get newest non-empty PLANNER_RESPONSE."""
    p = Path(transcript_path)
    full_p = p.with_name("transcript_full.jsonl")
    target = full_p if full_p.exists() else p
    if not target.exists():
        return -1, ""

    best_step = -1
    best_content = ""
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get("type") != "PLANNER_RESPONSE":
                    continue
                content = (entry.get("content") or "").strip()
                if not content:
                    continue
                step_idx = int(entry.get("step_index") if entry.get("step_index") is not None else -1)
                if step_idx > after_step_index and step_idx >= best_step:
                    best_step = step_idx
                    best_content = content
    except Exception as exc:
        hook_log(f"Failed reading transcript {target}: {exc}")
    return best_step, best_content


def _send_with_pacing(account: dict[str, Any], peer_id: str, text: str, context_token: str) -> tuple[bool, int, ILinkClient | None]:
    wait_sec = 0.0
    with locked_state() as st:
        now = time.time()
        if now < float(st.get("throttle_until") or 0.0):
            hook_log("In throttle cooldown; skipped sending.")
            return False, 0, None
        last_ts = float(st.get("last_send_ts") or 0.0)
        if now - last_ts < 1.5:
            wait_sec = 1.5 - (now - last_ts)
        st["last_send_ts"] = now + wait_sec
    if wait_sec > 0:
        time.sleep(wait_sec)

    client = ILinkClient(
        token=account.get("bot_token", ""),
        base_url=account.get("base_url") or DEFAULT_BASE_URL,
        cdn_base_url=account.get("cdn_base_url") or DEFAULT_CDN_BASE_URL,
        route_tag=account.get("route_tag", ""),
        use_system_proxy=bool(account.get("use_system_proxy", False)),
    )
    try:
        chunks = client.send_text(peer_id, text, context_token)
        with locked_state() as st:
            st["last_send_ts"] = time.time()
        return True, chunks, client
    except ILinkThrottledError as exc:
        with locked_state() as st:
            st["throttle_until"] = time.time() + THROTTLE_COOLDOWN_SEC
        hook_log(f"THROTTLED while sending: {exc}")
        return False, 0, client
    except Exception as exc:
        hook_log(f"Error sending to WeChat: {exc}")
        return False, 0, client


def _handle_watched_conversation(payload: dict[str, Any], conv_id: str, watch: dict[str, Any], state: dict[str, Any]) -> None:
    term_reason = (payload.get("terminationReason") or "").strip()
    fully_idle = bool(payload.get("fullyIdle", True))
    err_msg = (payload.get("error") or "").strip()
    transcript_path = (payload.get("transcriptPath") or "").strip()

    # Wait until the watched task is fully idle (unless it terminated with error/cancel).
    if not fully_idle and term_reason not in ("ERROR", "USER_CANCELED"):
        return

    last_sent = int(watch.get("last_sent_step_index", -1))
    step_idx, reply_text = extract_latest_reply(transcript_path, after_step_index=last_sent)
    if not reply_text and not err_msg:
        time.sleep(0.25)
        step_idx, reply_text = extract_latest_reply(transcript_path, after_step_index=last_sent)

    if term_reason == "USER_CANCELED":
        body = "该会话已被手动中止。"
    elif not reply_text and err_msg:
        body = f"⚠️ 执行异常中止（{term_reason}）：{err_msg[:500]}"
    elif reply_text:
        body = format_for_wechat(reply_text)
    else:
        body = f"该会话已空闲结束（原因: {term_reason or 'DONE'}）。"

    account = load_account()
    if not account:
        return
    peer_id = (watch.get("peer_id") or "").strip()
    context_token = (((state.get("peers") or {}).get(peer_id) or {}).get("context_token") or "").strip()
    if not peer_id or not context_token:
        hook_log(f"Watch alert skipped (missing context_token) conv={conv_id[:8]}")
        return

    proj = watch.get("project") or "未知项目"
    summary = watch.get("summary") or ""
    header = (
        f"🔔 盯梢任务已完成！\n"
        f"- 项目: {proj} ({conv_id[:8]})\n"
        f"- 会话: {summary}\n"
        "────────\n"
    )
    footer = f"\n\n（发 /bind {conv_id[:8]} 可直接在微信里追问该会话）"
    msg = header + body + footer

    ok, chunks, _ = _send_with_pacing(account, peer_id, msg, context_token)
    if ok:
        with locked_state() as st:
            st.setdefault("watched_conversations", {}).pop(conv_id, None)
            tr = (st.get("tracked_conversations") or {}).get(conv_id)
            if tr is not None and step_idx >= 0:
                tr["last_sent_step_index"] = max(int(tr.get("last_sent_step_index", -1)), step_idx)
        hook_log(f"Sent /watch alert for conv={conv_id[:8]} step={step_idx} chunks={chunks}")


def handle_stop(payload: dict[str, Any]) -> None:
    conv_id = (payload.get("conversationId") or "").strip()
    if not conv_id:
        return

    state = load_state()
    tracked = (state.get("tracked_conversations") or {}).get(conv_id)
    watched = (state.get("watched_conversations") or {}).get(conv_id)

    # Race guard if Stop arrives right as bridge.py registers a brand-new conversation.
    if tracked is None:
        last_input = (payload.get("lastUserInput") or "").strip()
        if last_input.startswith("[微信小助理消息"):
            time.sleep(0.5)
            state = load_state()
            tracked = (state.get("tracked_conversations") or {}).get(conv_id)
            watched = (state.get("watched_conversations") or {}).get(conv_id)

    if tracked is None:
        if watched is not None:
            _handle_watched_conversation(payload, conv_id, watched, state)
        return

    transcript_path = (payload.get("transcriptPath") or "").strip()
    last_sent = int(tracked.get("last_sent_step_index", -1))

    # Desktop-initiated turn inside a tracked conversation
    if not tracked.get("pending_from_wechat"):
        if watched is not None:
            _handle_watched_conversation(payload, conv_id, watched, state)
            return
        step_idx, _ = extract_latest_reply(transcript_path, after_step_index=last_sent)
        if step_idx > last_sent:
            with locked_state() as st:
                tr = st["tracked_conversations"].get(conv_id)
                if tr is not None and not tr.get("pending_from_wechat"):
                    tr["last_sent_step_index"] = max(int(tr.get("last_sent_step_index", -1)), step_idx)
        return

    term_reason = (payload.get("terminationReason") or "").strip()
    fully_idle = bool(payload.get("fullyIdle", True))
    err_msg = (payload.get("error") or "").strip()

    if term_reason == "USER_CANCELED":
        with locked_state() as st:
            tr = st["tracked_conversations"].get(conv_id)
            if tr is not None:
                tr["pending_from_wechat"] = False
                tr["typing_active"] = False
        return

    step_idx, reply_text = extract_latest_reply(transcript_path, after_step_index=last_sent)
    if not reply_text and not err_msg:
        time.sleep(0.25)
        step_idx, reply_text = extract_latest_reply(transcript_path, after_step_index=last_sent)

    if not reply_text and err_msg:
        reply_text = f"⚠️ Antigravity 执行异常中止（{term_reason}）：{err_msg[:500]}"
    elif not reply_text:
        if fully_idle:
            with locked_state() as st:
                tr = st["tracked_conversations"].get(conv_id)
                if tr is not None:
                    tr["pending_from_wechat"] = False
                    tr["typing_active"] = False
        return

    account = load_account()
    if not account:
        hook_log("No WeChat account configured; skipping reply.")
        return

    peer_id = (tracked.get("peer_id") or "").strip()
    peer_info = (state.get("peers") or {}).get(peer_id) or {}
    context_token = (peer_info.get("context_token") or "").strip()
    typing_ticket = (peer_info.get("typing_ticket") or "").strip()
    if not peer_id or not context_token:
        hook_log(f"Missing peer_id or context_token for conv={conv_id}")
        return

    formatted = format_for_wechat(reply_text)
    if not formatted:
        return

    ok, chunks, client = _send_with_pacing(account, peer_id, formatted, context_token)
    if ok:
        with locked_state() as st:
            tr = st["tracked_conversations"].get(conv_id)
            if tr is not None:
                if step_idx >= 0:
                    tr["last_sent_step_index"] = max(int(tr.get("last_sent_step_index", -1)), step_idx)
                if fully_idle:
                    tr["pending_from_wechat"] = False
                    tr["typing_active"] = False
            if fully_idle:
                st.setdefault("watched_conversations", {}).pop(conv_id, None)
        hook_log(f"Sent reply to {peer_id} conv={conv_id[:8]} step={step_idx} chunks={chunks} idle={fully_idle}")
        if fully_idle and typing_ticket and client is not None:
            try:
                client.send_typing(peer_id, typing_ticket, TYPING_STOP)
            except Exception:
                pass


def main() -> int:
    try:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
            if isinstance(payload, dict):
                handle_stop(payload)
    except Exception as exc:
        hook_log(f"Unhandled hook exception: {exc}")
    print("{}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
