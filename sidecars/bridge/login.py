"""Interactive QR login tool for WeChat ClawBot (iLink Bot API)."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import qrcode

from config_paths import (
    ACCOUNT_FILE,
    DEFAULT_BASE_URL,
    DEFAULT_CDN_BASE_URL,
    QR_IMAGE_FILE,
    save_account,
)
from ilink_client import ILinkClient


def open_image(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        print(f"[login] 无法自动打开图片窗口: {exc}", flush=True)


def render_qr(url: str, out_path: Path, copy_to: Path | None = None) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(str(out_path))
    if copy_to is not None:
        copy_to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out_path, copy_to)


def main() -> int:
    parser = argparse.ArgumentParser(description="WeChat ClawBot (iLink) QR Login")
    parser.add_argument("--no-open", action="store_true", help="Do not open QR image window automatically")
    parser.add_argument("--copy-qr", type=str, default="", help="Optional extra path to copy QR PNG")
    parser.add_argument("--timeout", type=int, default=420, help="Total wait timeout in seconds")
    args = parser.parse_args()

    client = ILinkClient()
    copy_path = Path(args.copy_qr) if args.copy_qr else None
    deadline = time.monotonic() + args.timeout

    while time.monotonic() < deadline:
        qrcode_id, qrcode_url = client.start_qr_login(bot_type="3")
        render_qr(qrcode_url, QR_IMAGE_FILE, copy_to=copy_path)

        print("========================================", flush=True)
        print("请用手机微信扫描二维码完成 ClawBot 绑定：", flush=True)
        print(f"二维码图片: {QR_IMAGE_FILE}", flush=True)
        if copy_path:
            print(f"副本路径:   {copy_path}", flush=True)
        print(f"二维码链接: {qrcode_url}", flush=True)
        print("========================================", flush=True)

        if not args.no_open:
            open_image(QR_IMAGE_FILE)

        scanned_notified = False
        while time.monotonic() < deadline:
            try:
                status_resp = client.poll_qr_status(qrcode_id)
            except Exception as exc:
                print(f"[login] 轮询扫码状态短暂异常，重试中: {exc}", flush=True)
                time.sleep(2.0)
                continue

            status = (status_resp.get("status") or "").strip().lower()
            if status == "scaned" and not scanned_notified:
                print("[login] 已扫码，请在手机微信上点击确认登录...", flush=True)
                scanned_notified = True
            elif status == "confirmed":
                bot_token = (status_resp.get("bot_token") or "").strip()
                ilink_bot_id = (status_resp.get("ilink_bot_id") or "").strip()
                base_url = (status_resp.get("baseurl") or DEFAULT_BASE_URL).strip()
                ilink_user_id = (status_resp.get("ilink_user_id") or "").strip()
                if not bot_token:
                    print(f"[login] 确认成功但未返回 bot_token: {status_resp}", file=sys.stderr, flush=True)
                    return 1

                account = {
                    "bot_token": bot_token,
                    "ilink_bot_id": ilink_bot_id,
                    "base_url": base_url,
                    "cdn_base_url": DEFAULT_CDN_BASE_URL,
                    "ilink_user_id": ilink_user_id,
                    "allow_from": [ilink_user_id] if ilink_user_id else ["*"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                save_account(account)
                print("[login] 绑定成功！凭证已保存。", flush=True)
                print(f"[login] account_file={ACCOUNT_FILE}", flush=True)
                print(f"[login] ilink_bot_id={ilink_bot_id}", flush=True)
                print(f"[login] allow_from={account['allow_from']}", flush=True)
                return 0
            elif status == "expired":
                print("[login] 二维码已过期，正在刷新新二维码...", flush=True)
                break
            time.sleep(2.0)

    print("[login] 等待扫码超时，请重新运行 login.py。", file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
