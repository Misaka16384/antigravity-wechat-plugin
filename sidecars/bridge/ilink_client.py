"""Official WeChat ClawBot (iLink Bot API) client with AES-128-ECB CDN media support."""

from __future__ import annotations

import base64
import hashlib
import io
import mimetypes
import os
import re
import secrets
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.parse
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from config_paths import DATA_DIR, DEFAULT_BASE_URL, DEFAULT_CDN_BASE_URL, PLUGIN_DIR

CHANNEL_VERSION = "antigravity-wechat-sidecar/1.0"

MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MSG_STATE_FINISH = 2
SESSION_EXPIRED_ERRCODE = -14

UPLOAD_MEDIA_IMAGE = 1
UPLOAD_MEDIA_VIDEO = 2
UPLOAD_MEDIA_FILE = 3

TYPING_START = 1
TYPING_STOP = 2

MAX_TEXT_CHUNK_CHARS = 3500
MIN_CHUNK_INTERVAL_SEC = 1.6
MAX_MEDIA_BYTES = 100 * 1024 * 1024  # 100 MiB

_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_WIN_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ILinkError(RuntimeError):
    def __init__(self, message: str, ret: int = 0, errcode: int = 0):
        super().__init__(message)
        self.ret = ret
        self.errcode = errcode


class ILinkSessionExpiredError(ILinkError):
    pass


class ILinkThrottledError(ILinkError):
    pass


def random_wechat_uin() -> str:
    val = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(val).encode("ascii")).decode("ascii")


def parse_aes_key(aes_key_b64: str) -> bytes:
    """Decode iLink AES-128 key: supports base64(16 raw bytes) or base64(32-char hex)."""
    raw = base64.b64decode(aes_key_b64.strip())
    if len(raw) == 16:
        return raw
    if len(raw) == 32:
        try:
            text = raw.decode("ascii")
            if _HEX32_RE.match(text):
                return bytes.fromhex(text)
        except UnicodeDecodeError:
            pass
    raise ValueError(f"Unsupported iLink aes_key length after base64 decode: {len(raw)}")


def format_aes_key_for_api(key: bytes) -> str:
    """Encode 16-byte raw AES key as base64(hex_string) for iLink sendMessage API."""
    return base64.b64encode(key.hex().encode("ascii")).decode("ascii")


def aes_ecb_padded_size(plaintext_len: int) -> int:
    return ((plaintext_len // 16) + 1) * 16


def encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


def decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    dec = cipher.decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def detect_image_ext(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _find_bin(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if os.name != "nt":
        for d in (
            "/opt/homebrew/bin",
            "/usr/local/bin",
            str(Path.home() / ".volta" / "bin"),
        ):
            cand = Path(d) / name
            if cand.exists():
                return str(cand)
        nvm_root = Path.home() / ".nvm" / "versions" / "node"
        if nvm_root.exists():
            for cand in sorted(nvm_root.glob(f"*/bin/{name}"), reverse=True):
                if cand.exists():
                    return str(cand)
    return None


def decode_voice_bytes(data: bytes, sample_rate: int = 24000) -> tuple[bytes, str]:
    """Convert inbound WeChat voice bytes (typically SILK v3) to a playable audio format.

    Decoder priority for SILK v3:
      1. `silk-wasm` via Node.js (`scripts/silk-decode.mjs`) — pure WebAssembly,
         zero compiler needed across Windows, macOS (Apple Silicon & Intel), and Linux.
      2. Python `pilk` (if installed).
      3. `ffmpeg` fallback for non-SILK formats such as AMR.
    """
    if not data:
        raise ValueError("Empty voice payload")
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return data, ".wav"
    if data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return data, ".mp3"
    if data.startswith(b"OggS"):
        return data, ".ogg"
    if data.startswith(b"fLaC"):
        return data, ".flac"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return data, ".m4a"

    rate = sample_rate if sample_rate in (8000, 12000, 16000, 24000, 32000, 44100, 48000) else 24000
    errors: list[str] = []

    # WeChat voice is usually SILK v3 (often prefixed with \x02#!SILK_V3 or #!SILK_V3)
    if b"#!SILK_V3" in data[:16]:
        # 1. Preferred: cross-platform WebAssembly decoder (`silk-wasm`)
        silk_script = PLUGIN_DIR / "scripts" / "silk-decode.mjs"
        node_bin = _find_bin("node")
        if silk_script.exists() and node_bin:
            with tempfile.TemporaryDirectory(prefix="wechat_voice_") as td:
                silk_path = os.path.join(td, "in.silk")
                wav_path = os.path.join(td, "out.wav")
                Path(silk_path).write_bytes(data)
                proc = subprocess.run(
                    [node_bin, str(silk_script), silk_path, wav_path, str(rate)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    creationflags=_NO_WINDOW,
                    env={
                        **os.environ,
                        "WECHAT_ASSISTANT_DATA_DIR": str(DATA_DIR),
                        "PLUGIN_DATA": str(DATA_DIR),
                    },
                )
                if proc.returncode == 0 and os.path.exists(wav_path):
                    wav_bytes = Path(wav_path).read_bytes()
                    if len(wav_bytes) > 44 and wav_bytes[:4] == b"RIFF":
                        return wav_bytes, ".wav"
                errors.append(f"silk-wasm: {(proc.stderr or proc.stdout).strip()[:160]}")

        # 2. Optional fallback: Python `pilk`
        try:
            import pilk  # type: ignore[import-not-found]

            with tempfile.TemporaryDirectory(prefix="wechat_voice_") as td:
                silk_path = os.path.join(td, "in.silk")
                pcm_path = os.path.join(td, "out.pcm")
                Path(silk_path).write_bytes(data)
                pilk.decode(silk_path, pcm_path, pcm_rate=rate)
                pcm_bytes = Path(pcm_path).read_bytes()
            if pcm_bytes:
                buf = io.BytesIO()
                with wave.open(buf, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(rate)
                    wf.writeframes(pcm_bytes)
                return buf.getvalue(), ".wav"
            errors.append("pilk: decoded empty PCM")
        except Exception as exc:
            errors.append(f"pilk: {exc}")

    # 3. Fallback: transcode other audio formats (e.g. AMR) via ffmpeg if available
    ffmpeg_bin = _find_bin("ffmpeg")
    if ffmpeg_bin:
        with tempfile.TemporaryDirectory(prefix="wechat_voice_") as td:
            in_path = os.path.join(td, "in.bin")
            out_path = os.path.join(td, "out.wav")
            Path(in_path).write_bytes(data)
            proc = subprocess.run(
                [ffmpeg_bin, "-y", "-loglevel", "error", "-i", in_path, "-ar", str(rate), "-ac", "1", out_path],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=_NO_WINDOW,
            )
            if proc.returncode == 0 and os.path.exists(out_path):
                wav_bytes = Path(out_path).read_bytes()
                if wav_bytes:
                    return wav_bytes, ".wav"
            errors.append(f"ffmpeg(rc={proc.returncode}): {proc.stderr.strip()[:160]}")

    detail = "; ".join(errors) if errors else f"header={data[:16]!r}"
    raise ValueError(f"Unable to decode voice audio ({detail})")


def sanitize_filename(name: str, fallback: str = "attachment.bin") -> str:
    cleaned = _WIN_INVALID_CHARS.sub("_", (name or "").strip()).strip(". ")
    if not cleaned:
        return fallback
    return cleaned[:160]


def split_text_chunks(text: str, max_chars: int = MAX_TEXT_CHUNK_CHARS) -> list[str]:
    """Split long replies cleanly on paragraph or line boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = window.rfind("\n\n")
        if cut < max_chars // 3:
            cut = window.rfind("\n")
        if cut < max_chars // 3:
            cut = window.rfind(" ")
        if cut < max_chars // 3:
            cut = max_chars
        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


class ILinkClient:
    """HTTP client for Tencent iLink Bot API and CDN media transport."""

    def __init__(
        self,
        token: str = "",
        base_url: str = DEFAULT_BASE_URL,
        cdn_base_url: str = DEFAULT_CDN_BASE_URL,
        route_tag: str = "",
        use_system_proxy: bool = False,
    ) -> None:
        self.token = (token or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/") + "/"
        self.cdn_base_url = (cdn_base_url or DEFAULT_CDN_BASE_URL).rstrip("/")
        self.route_tag = (route_tag or "").strip()

        self.session = requests.Session()
        # Domestic Tencent endpoints work best directly without overseas proxy hops.
        self.session.trust_env = use_system_proxy

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": random_wechat_uin(),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.route_tag:
            headers["SKRouteTag"] = self.route_tag
        return headers

    def _post_json(self, endpoint: str, payload: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
        url = self.base_url + endpoint.lstrip("/")
        resp = self.session.post(url, json=payload, headers=self._headers(), timeout=timeout)
        if resp.status_code != 200:
            raise ILinkError(f"POST {endpoint} HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.text.strip()
        if not body:
            return {}
        return resp.json()

    # ── QR Login ──────────────────────────────────────────────────────────────

    def start_qr_login(self, bot_type: str = "3") -> tuple[str, str]:
        url = self.base_url + f"ilink/bot/get_bot_qrcode?bot_type={urllib.parse.quote(bot_type)}"
        resp = self.session.get(url, headers={"X-WECHAT-UIN": random_wechat_uin()}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        qrcode_id = (data.get("qrcode") or "").strip()
        qrcode_url = (data.get("qrcode_img_content") or "").strip()
        if data.get("ret", 0) != 0 or not qrcode_id or not qrcode_url:
            raise ILinkError(f"get_bot_qrcode failed: {data}", ret=int(data.get("ret") or 0))
        return qrcode_id, qrcode_url

    def poll_qr_status(self, qrcode_id: str) -> dict[str, Any]:
        url = self.base_url + f"ilink/bot/get_qrcode_status?qrcode={urllib.parse.quote(qrcode_id)}"
        headers = {
            "iLink-App-ClientVersion": "1",
            "X-WECHAT-UIN": random_wechat_uin(),
        }
        resp = self.session.get(url, headers=headers, timeout=40)
        resp.raise_for_status()
        return resp.json()

    # ── Long Polling ──────────────────────────────────────────────────────────

    def get_updates(self, get_updates_buf: str = "", timeout_ms: int = 35000) -> dict[str, Any]:
        payload = {
            "get_updates_buf": get_updates_buf or "",
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        try:
            data = self._post_json("ilink/bot/getupdates", payload, timeout=(timeout_ms / 1000.0) + 8.0)
        except requests.Timeout:
            return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf}

        ret = int(data.get("ret") or 0)
        errcode = int(data.get("errcode") or 0)
        if errcode == SESSION_EXPIRED_ERRCODE or ret == SESSION_EXPIRED_ERRCODE:
            raise ILinkSessionExpiredError(
                f"iLink session expired (ret={ret}, errcode={errcode}, errmsg={data.get('errmsg', '')})",
                ret=ret,
                errcode=errcode,
            )
        if ret != 0 or errcode != 0:
            raise ILinkError(
                f"getupdates error (ret={ret}, errcode={errcode}, errmsg={data.get('errmsg', '')})",
                ret=ret,
                errcode=errcode,
            )
        return data

    # ── Typing Indicator ──────────────────────────────────────────────────────

    def get_config(self, user_id: str, context_token: str) -> str:
        payload = {
            "ilink_user_id": user_id,
            "context_token": context_token,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        data = self._post_json("ilink/bot/getconfig", payload, timeout=10.0)
        if int(data.get("ret") or 0) != 0 or int(data.get("errcode") or 0) != 0:
            return ""
        return (data.get("typing_ticket") or "").strip()

    def send_typing(self, user_id: str, typing_ticket: str, status: int = TYPING_START) -> None:
        if not user_id or not typing_ticket:
            return
        payload = {
            "ilink_user_id": user_id,
            "typing_ticket": typing_ticket,
            "status": status,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        self._post_json("ilink/bot/sendtyping", payload, timeout=10.0)

    # ── Outbound Messages ─────────────────────────────────────────────────────

    def send_raw_items(self, to_user_id: str, context_token: str, item_list: list[dict[str, Any]]) -> None:
        if not context_token:
            raise ILinkError("context_token is required to send messages via iLink")
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"agy-{secrets.token_hex(8)}",
                "message_type": MSG_TYPE_BOT,
                "message_state": MSG_STATE_FINISH,
                "item_list": item_list,
                "context_token": context_token,
            },
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        data = self._post_json("ilink/bot/sendmessage", payload, timeout=20.0)
        ret = int(data.get("ret") or 0)
        errcode = int(data.get("errcode") or 0)
        if ret == -2 or errcode == -2:
            raise ILinkThrottledError(
                f"iLink sendmessage throttled (ret={ret}, errmsg={data.get('errmsg', '')})",
                ret=ret,
                errcode=errcode,
            )
        if ret != 0 or errcode != 0:
            raise ILinkError(
                f"sendmessage failed (ret={ret}, errcode={errcode}, errmsg={data.get('errmsg', '')})",
                ret=ret,
                errcode=errcode,
            )

    def send_text(self, to_user_id: str, text: str, context_token: str) -> int:
        chunks = split_text_chunks(text)
        for idx, chunk in enumerate(chunks):
            if idx > 0:
                time.sleep(MIN_CHUNK_INTERVAL_SEC)
            self.send_raw_items(
                to_user_id=to_user_id,
                context_token=context_token,
                item_list=[{"type": ITEM_TEXT, "text_item": {"text": chunk}}],
            )
        return len(chunks)

    # ── CDN Download & Inbound Parsing ────────────────────────────────────────

    def download_cdn_bytes(self, encrypted_query_param: str, aes_key_b64: str | None = None) -> bytes:
        url = f"{self.cdn_base_url}/download?encrypted_query_param={urllib.parse.quote(encrypted_query_param, safe='')}"
        resp = self.session.get(url, timeout=90.0)
        if resp.status_code != 200:
            raise ILinkError(f"CDN download HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.content
        if len(data) > MAX_MEDIA_BYTES:
            raise ILinkError(f"CDN media exceeds {MAX_MEDIA_BYTES} bytes")
        if aes_key_b64:
            key = parse_aes_key(aes_key_b64)
            return decrypt_aes_ecb(data, key)
        return data

    def save_inbox_file(self, inbox_dir: Path, filename: str, content: bytes) -> Path:
        day_dir = inbox_dir / datetime.now().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        safe = sanitize_filename(filename)
        target = day_dir / f"{stamp}_{safe}"
        counter = 1
        while target.exists():
            stem = target.stem
            suffix = target.suffix
            target = day_dir / f"{stamp}_{counter}_{Path(safe).stem}{suffix}"
            counter += 1
        target.write_bytes(content)
        return target

    @staticmethod
    def _extract_text_from_items(items: list[dict[str, Any]], include_voice_asr: bool = True) -> list[str]:
        parts: list[str] = []
        for item in items:
            t = int(item.get("type") or 0)
            if t == ITEM_TEXT:
                text = ((item.get("text_item") or {}).get("text") or "").strip()
                ref = item.get("ref_msg")
                if ref:
                    ref_parts: list[str] = []
                    title = (ref.get("title") or "").strip()
                    if title:
                        ref_parts.append(title)
                    ref_item = ref.get("message_item")
                    if isinstance(ref_item, dict):
                        ref_type = int(ref_item.get("type") or 0)
                        if ref_type == ITEM_TEXT:
                            rt = ((ref_item.get("text_item") or {}).get("text") or "").strip()
                            if rt:
                                ref_parts.append(rt)
                        elif ref_type == ITEM_FILE:
                            fn = ((ref_item.get("file_item") or {}).get("file_name") or "").strip()
                            if fn:
                                ref_parts.append(f"文件: {fn}")
                        elif ref_type == ITEM_IMAGE:
                            ref_parts.append("图片")
                    if ref_parts:
                        prefix = f"[引用: {' | '.join(ref_parts)}]"
                        parts.append(f"{prefix}\n{text}" if text else prefix)
                        continue
                if text:
                    parts.append(text)
            elif t == ITEM_VOICE and include_voice_asr:
                asr = ((item.get("voice_item") or {}).get("text") or "").strip()
                if asr:
                    parts.append(f"[语音转文字] {asr}")
        return parts

    def parse_and_download_inbound(
        self, msg: dict[str, Any], inbox_dir: Path
    ) -> tuple[str, list[dict[str, str]], list[str]]:
        """Return (combined_text, saved_attachments, warnings)."""
        items = msg.get("item_list") or []
        text_parts = self._extract_text_from_items(items, include_voice_asr=False)
        attachments: list[dict[str, str]] = []
        warnings: list[str] = []
        seen_enc: set[str] = set()

        for idx, item in enumerate(items):
            t = int(item.get("type") or 0)
            if t == ITEM_IMAGE:
                img = item.get("image_item") or {}
                media = img.get("media") or img.get("cdn_media") or {}
                enc = (media.get("encrypt_query_param") or "").strip()
                if not enc or enc in seen_enc:
                    continue
                seen_enc.add(enc)
                aes_b64: str | None = None
                aes_hex = (img.get("aeskey") or "").strip()
                if aes_hex and _HEX32_RE.match(aes_hex):
                    aes_b64 = base64.b64encode(bytes.fromhex(aes_hex)).decode("ascii")
                elif media.get("aes_key"):
                    aes_b64 = str(media["aes_key"]).strip()
                try:
                    raw = self.download_cdn_bytes(enc, aes_b64)
                    ext = detect_image_ext(raw)
                    saved = self.save_inbox_file(inbox_dir, f"image_{idx}{ext}", raw)
                    attachments.append({"kind": "image", "path": str(saved), "name": saved.name})
                except Exception as exc:
                    warnings.append(f"图片下载失败: {exc}")

            elif t == ITEM_FILE:
                f_item = item.get("file_item") or {}
                media = f_item.get("media") or f_item.get("cdn_media") or {}
                enc = (media.get("encrypt_query_param") or "").strip()
                aes_b64 = (media.get("aes_key") or "").strip() or None
                if not enc or enc in seen_enc:
                    continue
                seen_enc.add(enc)
                fname = (f_item.get("file_name") or "").strip() or f"file_{idx}.bin"
                try:
                    raw = self.download_cdn_bytes(enc, aes_b64)
                    saved = self.save_inbox_file(inbox_dir, fname, raw)
                    attachments.append({"kind": "file", "path": str(saved), "name": fname})
                except Exception as exc:
                    warnings.append(f"文件 {fname} 下载失败: {exc}")

            elif t == ITEM_VIDEO:
                v_item = item.get("video_item") or {}
                media = v_item.get("media") or v_item.get("cdn_media") or {}
                enc = (media.get("encrypt_query_param") or "").strip()
                aes_b64 = (media.get("aes_key") or "").strip() or None
                if not enc or enc in seen_enc:
                    continue
                seen_enc.add(enc)
                try:
                    raw = self.download_cdn_bytes(enc, aes_b64)
                    saved = self.save_inbox_file(inbox_dir, f"video_{idx}.mp4", raw)
                    attachments.append({"kind": "video", "path": str(saved), "name": saved.name})
                except Exception as exc:
                    warnings.append(f"视频下载失败: {exc}")

            elif t == ITEM_VOICE:
                v_item = item.get("voice_item") or {}
                asr = (v_item.get("text") or "").strip()
                media = v_item.get("media") or v_item.get("cdn_media") or {}
                enc = (media.get("encrypt_query_param") or "").strip()
                aes_b64: str | None = None
                aes_hex = (v_item.get("aeskey") or "").strip()
                if aes_hex and _HEX32_RE.match(aes_hex):
                    aes_b64 = base64.b64encode(bytes.fromhex(aes_hex)).decode("ascii")
                elif media.get("aes_key"):
                    aes_b64 = str(media["aes_key"]).strip()

                if enc and enc not in seen_enc:
                    seen_enc.add(enc)
                    raw: bytes | None = None
                    try:
                        raw = self.download_cdn_bytes(enc, aes_b64)
                        sample_rate = int(v_item.get("sample_rate") or 24000)
                        audio_bytes, ext = decode_voice_bytes(raw, sample_rate=sample_rate)
                        saved = self.save_inbox_file(inbox_dir, f"voice_{idx}{ext}", audio_bytes)
                        attachments.append({"kind": "audio", "path": str(saved), "name": saved.name})
                        continue
                    except Exception as exc:
                        if asr:
                            text_parts.append(f"[语音转文字] {asr}")
                            warnings.append(f"语音原声下载/解码失败（已回退到语音转文字）: {exc}")
                        else:
                            if raw:
                                saved = self.save_inbox_file(inbox_dir, f"voice_{idx}.silk", raw)
                                attachments.append({"kind": "voice", "path": str(saved), "name": saved.name})
                            warnings.append(f"语音原声下载/解码失败: {exc}")
                        continue

                if asr:
                    text_parts.append(f"[语音转文字] {asr}")
                elif not enc:
                    warnings.append("收到语音消息，但既无音频链接也无转文字内容")

        return "\n".join(text_parts).strip(), attachments, warnings

    # ── CDN Outbound Upload (Send Image / File back to WeChat) ────────────────

    def _upload_to_cdn(self, to_user_id: str, plaintext: bytes, media_type: int) -> dict[str, Any]:
        if not plaintext:
            raise ILinkError("Cannot upload empty file")
        aes_key = secrets.token_bytes(16)
        filekey = secrets.token_hex(16)
        rawsize = len(plaintext)
        filesize = aes_ecb_padded_size(rawsize)
        raw_md5 = hashlib.md5(plaintext).hexdigest()

        req_body = {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": raw_md5,
            "filesize": filesize,
            "no_need_thumb": True,
            "aeskey": aes_key.hex(),
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        up_resp = self._post_json("ilink/bot/getuploadurl", req_body, timeout=20.0)
        upload_full_url = (up_resp.get("upload_full_url") or "").strip()
        upload_param = (up_resp.get("upload_param") or "").strip()
        if not upload_full_url and not upload_param:
            raise ILinkError(f"getuploadurl returned no upload URL: {up_resp}")

        if upload_full_url:
            cdn_url = upload_full_url
        else:
            cdn_url = (
                f"{self.cdn_base_url}/upload?encrypted_query_param="
                f"{urllib.parse.quote(upload_param, safe='')}&filekey={urllib.parse.quote(filekey, safe='')}"
            )

        ciphertext = encrypt_aes_ecb(plaintext, aes_key)
        resp = self.session.post(
            cdn_url,
            data=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
            timeout=120.0,
        )
        if resp.status_code != 200:
            err_msg = resp.headers.get("x-error-message") or resp.text[:200]
            raise ILinkError(f"CDN upload HTTP {resp.status_code}: {err_msg}")
        dl_param = (resp.headers.get("x-encrypted-param") or "").strip()
        if not dl_param:
            raise ILinkError("CDN upload response missing x-encrypted-param header")

        return {
            "download_param": dl_param,
            "aes_key_api": format_aes_key_for_api(aes_key),
            "raw_size": rawsize,
            "cipher_size": filesize,
        }

    def send_image_file(self, to_user_id: str, context_token: str, image_path: Path) -> None:
        raw = image_path.read_bytes()
        ref = self._upload_to_cdn(to_user_id, raw, UPLOAD_MEDIA_IMAGE)
        item = {
            "type": ITEM_IMAGE,
            "image_item": {
                "media": {
                    "encrypt_query_param": ref["download_param"],
                    "aes_key": ref["aes_key_api"],
                    "encrypt_type": 1,
                },
                "mid_size": ref["cipher_size"],
            },
        }
        self.send_raw_items(to_user_id, context_token, [item])

    def send_attachment_file(self, to_user_id: str, context_token: str, file_path: Path) -> None:
        raw = file_path.read_bytes()
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if mime_type and mime_type.startswith("video/"):
            ref = self._upload_to_cdn(to_user_id, raw, UPLOAD_MEDIA_VIDEO)
            item = {
                "type": ITEM_VIDEO,
                "video_item": {
                    "media": {
                        "encrypt_query_param": ref["download_param"],
                        "aes_key": ref["aes_key_api"],
                        "encrypt_type": 1,
                    },
                    "video_size": ref["cipher_size"],
                },
            }
        else:
            ref = self._upload_to_cdn(to_user_id, raw, UPLOAD_MEDIA_FILE)
            item = {
                "type": ITEM_FILE,
                "file_item": {
                    "media": {
                        "encrypt_query_param": ref["download_param"],
                        "aes_key": ref["aes_key_api"],
                        "encrypt_type": 1,
                    },
                    "file_name": file_path.name,
                    "len": str(ref["raw_size"]),
                },
            }
        self.send_raw_items(to_user_id, context_token, [item])
