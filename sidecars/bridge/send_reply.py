"""CLI helper allowing Antigravity to proactively send text, images, or files back to WeChat."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from config_paths import DEFAULT_BASE_URL, DEFAULT_CDN_BASE_URL, load_account, load_state, locked_state
from ilink_client import ILinkClient, ILinkThrottledError

THROTTLE_COOLDOWN_SEC = 1800.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Send text/image/file to bound WeChat user via iLink")
    parser.add_argument("--text", type=str, default="", help="Text message to send")
    parser.add_argument("--image", type=str, default="", help="Local image file path to send")
    parser.add_argument("--file", type=str, default="", help="Local file/document/video path to send")
    parser.add_argument("--peer", type=str, default="", help="Optional target ilink_user_id (defaults to bound user)")
    args = parser.parse_args()

    if not any([args.text, args.image, args.file]):
        print("Error: specify at least one of --text, --image, or --file", file=sys.stderr)
        return 2

    account = load_account()
    if not account:
        print("Error: WeChat account not logged in yet (run login.py first)", file=sys.stderr)
        return 1

    state = load_state()
    peers = state.get("peers") or {}
    peer_id = args.peer.strip() or (account.get("ilink_user_id") or "").strip()
    if not peer_id and peers:
        peer_id = next(iter(peers.keys()))

    context_token = ((peers.get(peer_id) or {}).get("context_token") or "").strip()
    if not peer_id or not context_token:
        print(
            "Error: missing context_token — please send at least one message to your ClawBot in WeChat first.",
            file=sys.stderr,
        )
        return 1

    client = ILinkClient(
        token=account.get("bot_token", ""),
        base_url=account.get("base_url") or DEFAULT_BASE_URL,
        cdn_base_url=account.get("cdn_base_url") or DEFAULT_CDN_BASE_URL,
        route_tag=account.get("route_tag", ""),
        use_system_proxy=bool(account.get("use_system_proxy", False)),
    )

    wait_sec = 0.0
    with locked_state() as st:
        now = time.time()
        if now < float(st.get("throttle_until") or 0.0):
            print("Error: iLink send is currently in rate-limit cooldown.", file=sys.stderr)
            return 1
        last_ts = float(st.get("last_send_ts") or 0.0)
        if now - last_ts < 1.5:
            wait_sec = 1.5 - (now - last_ts)
        st["last_send_ts"] = now + wait_sec
    if wait_sec > 0:
        time.sleep(wait_sec)

    try:
        if args.text:
            client.send_text(peer_id, args.text, context_token)
            print(f"Sent text ({len(args.text)} chars) to {peer_id}")
            if args.image or args.file:
                time.sleep(1.6)
        if args.image:
            img_path = Path(args.image)
            if not img_path.is_file():
                print(f"Error: image not found: {img_path}", file=sys.stderr)
                return 1
            client.send_image_file(peer_id, context_token, img_path)
            print(f"Sent image {img_path.name} to {peer_id}")
            if args.file:
                time.sleep(1.6)
        if args.file:
            file_path = Path(args.file)
            if not file_path.is_file():
                print(f"Error: file not found: {file_path}", file=sys.stderr)
                return 1
            client.send_attachment_file(peer_id, context_token, file_path)
            print(f"Sent file {file_path.name} to {peer_id}")
        with locked_state() as st:
            st["last_send_ts"] = time.time()
    except ILinkThrottledError as exc:
        with locked_state() as st:
            st["throttle_until"] = time.time() + THROTTLE_COOLDOWN_SEC
        print(f"Error: throttled by iLink: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error sending via iLink: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
