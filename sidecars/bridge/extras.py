"""Extended helpers for wechat-assistant: projects, screenshots, system info, and daily digest."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from config_paths import DEFAULT_INBOX_DIR, SIDECAR_DIR, SIDECAR_ID, load_state

CONFIG_FILE = Path.home() / ".gemini" / "config" / "config.json"
PROJECTS_DIR = Path.home() / ".gemini" / "config" / "projects"
DIGEST_DIR = Path.home() / ".gemini" / "antigravity" / "scratch" / "daily-digest"
_SKIP_PROJECT_IDS = {"default-cli-project"}
_SKIP_PROJECT_NAMES = {"CLI Project", "swarm/swarm_board"}


def _decode_file_uri(uri: str) -> str:
    if not uri:
        return ""
    u = uri.strip()
    if u.lower().startswith("file:///"):
        u = u[8:] if os.name == "nt" else u[7:]
    elif u.lower().startswith("file://"):
        u = u[7:]
    u = urllib.parse.unquote(u)
    if os.name == "nt":
        u = u.replace("/", "\\")
        if len(u) >= 2 and u[1] == ":":
            u = u[0].upper() + u[1:]
    return u


def _extract_project_folder_uris(proj_obj: dict[str, Any]) -> list[str]:
    uris: list[str] = []
    resources = (proj_obj.get("projectResources") or {}).get("resources") or []
    for r in resources:
        if not isinstance(r, dict):
            continue
        uri = (r.get("folderUri") or "").strip()
        if not uri and isinstance(r.get("gitFolder"), dict):
            uri = (r["gitFolder"].get("folderUri") or "").strip()
        if uri and uri not in uris:
            uris.append(uri)
    return uris


def get_default_project_id() -> str:
    state = load_state()
    pid = (state.get("default_project_id") or "").strip()
    if pid:
        return pid
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            entry = (cfg.get("sidecars") or {}).get(SIDECAR_ID) or {}
            cfg_pid = str(entry.get("projectId") or "").strip()
            if cfg_pid:
                return cfg_pid
        except Exception:
            pass
    return ""


def list_user_projects() -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    if not PROJECTS_DIR.exists():
        return projects
    default_pid = get_default_project_id()
    for pf in PROJECTS_DIR.glob("*.json"):
        try:
            obj = json.loads(pf.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = str(obj.get("id") or pf.stem).strip()
        name = str(obj.get("name") or pid[:8]).strip()
        if pid in _SKIP_PROJECT_IDS or name in _SKIP_PROJECT_NAMES:
            continue
        uris = _extract_project_folder_uris(obj)
        folders = [_decode_file_uri(u) for u in uris if u]
        projects.append(
            {
                "id": pid,
                "name": name,
                "folder_uris": uris,
                "folders": folders,
            }
        )

    def _sort_key(p: dict[str, Any]) -> tuple[int, str]:
        if (default_pid and p["id"] == default_pid) or p["name"].lower() in ("wechat assistant", "wechatinbox"):
            return (0, "")
        if p["id"] == "outside-of-project":
            return (2, "")
        return (1, p["name"].lower())

    projects.sort(key=_sort_key)
    return projects


def resolve_project(
    query: str, listed_ids: list[str] | None = None
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Resolve user query (index / 'default' / name / substring / id prefix) to a project."""
    projects = list_user_projects()
    by_id = {p["id"]: p for p in projects}
    q = (query or "").strip()
    if not q:
        return None, []

    if q.lower() in ("default", "home", "wechat", "reset"):
        default_pid = get_default_project_id()
        if default_pid and default_pid in by_id:
            return by_id[default_pid], []
        for p in projects:
            if p["name"].lower() in ("wechat assistant", "wechatinbox"):
                return p, []
        if "outside-of-project" in by_id:
            return by_id["outside-of-project"], []
        if projects:
            return projects[0], []

    if q.isdigit():
        idx = int(q)
        ids = listed_ids if listed_ids else [p["id"] for p in projects]
        if 1 <= idx <= len(ids) and ids[idx - 1] in by_id:
            return by_id[ids[idx - 1]], []
        return None, []

    ql = q.lower()
    for p in projects:
        if p["id"].lower() == ql or p["name"].lower() == ql:
            return p, []
    matches = [
        p for p in projects if p["id"].lower().startswith(ql) or ql in p["name"].lower()
    ]
    if len(matches) == 1:
        return matches[0], []
    return None, matches


def switch_sidecar_project_in_config(target_proj: dict[str, Any]) -> None:
    """Update sidecars[SIDECAR_ID].projectId and workspaceUris in global config.json."""
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Antigravity config.json not found at {CONFIG_FILE}")
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    sidecars = cfg.setdefault("sidecars", {})
    entry = sidecars.setdefault(SIDECAR_ID, {"enabled": True, "schemaVersion": 1})
    entry["projectId"] = target_proj["id"]

    state = load_state()
    inbox_dir = Path(state.get("inbox_dir") or str(DEFAULT_INBOX_DIR)).expanduser().resolve()
    inbox_dir.mkdir(parents=True, exist_ok=True)
    send_reply_script = SIDECAR_DIR / "send_reply.py"

    base_grants = [
        f"read_file({inbox_dir})",
        f"write_file({inbox_dir})",
        f"command(python {send_reply_script})",
    ]
    for folder in target_proj.get("folders") or []:
        if folder:
            rf = f"read_file({folder})"
            wf = f"write_file({folder})"
            if rf not in base_grants:
                base_grants.append(rf)
            if wf not in base_grants:
                base_grants.append(wf)

    uris = list(target_proj.get("folder_uris") or [])
    inbox_uri = inbox_dir.as_uri()
    if inbox_uri not in uris:
        uris.append(inbox_uri)

    entry["resolvedPermissions"] = {
        "accessGrants": base_grants,
        "workspaceUris": uris,
    }
    tmp = CONFIG_FILE.with_suffix(f".json.{os.getpid()}.tmp")
    raw = json.dumps(cfg, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_FILE)


# ── Screenshot & System Status ────────────────────────────────────────────────


def is_windows_locked() -> bool:
    if os.name != "nt":
        return False
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq LogonUI.exe", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        return "LogonUI.exe" in (proc.stdout or "")
    except Exception:
        return False


def capture_desktop_screenshot(out_path: Path, max_width: int = 1920) -> tuple[bool, str]:
    """Capture desktop screenshot into JPEG. Returns (ok, detail_or_error)."""
    if is_windows_locked():
        return False, "locked"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # macOS native screencapture fallback when Pillow ImageGrab lacks permission
    if sys.platform == "darwin" and shutil.which("screencapture"):
        try:
            proc = subprocess.run(["screencapture", "-x", "-t", "jpg", str(out_path)], timeout=10)
            if proc.returncode == 0 and out_path.exists():
                return True, "macOS screencapture"
        except Exception:
            pass

    try:
        from PIL import Image, ImageGrab

        try:
            img = ImageGrab.grab(all_screens=True)
        except Exception:
            img = ImageGrab.grab()
        orig_w, orig_h = img.size
        if img.mode != "RGB":
            img = img.convert("RGB")
        if orig_w > max_width:
            new_h = int(orig_h * (max_width / orig_w))
            img = img.resize((max_width, new_h), Image.Resampling.LANCZOS)
        img.save(str(out_path), format="JPEG", quality=85, optimize=True)
        return True, f"{img.size[0]}x{img.size[1]} (原屏 {orig_w}x{orig_h})"
    except Exception as exc:
        return False, str(exc)


def collect_system_status() -> str:
    lines = [f"电脑实时状态 ({platform.system()} {platform.release()})："]

    if os.name == "nt":
        locked = is_windows_locked()
        lines.append(f"- 锁屏状态: {'已锁屏 (LogonUI)' if locked else '已解锁 (桌面活跃)'}")
        ps_script = (
            "$os = Get-CimInstance Win32_OperatingSystem; "
            "$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average; "
            "$totMem = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1); "
            "$freeMem = [math]::Round($os.FreePhysicalMemory / 1MB, 1); "
            "$usedMem = [math]::Round($totMem - $freeMem, 1); "
            "$up = (Get-Date) - $os.LastBootUpTime; "
            "$upStr = '{0}天{1}小时{2}分' -f $up.Days, $up.Hours, $up.Minutes; "
            "$wins = (Get-Process | Where-Object { $_.MainWindowTitle -ne '' } | Select-Object -First 8 -ExpandProperty MainWindowTitle) -join ' | '; "
            "$top = (Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 5 | ForEach-Object { '{0}({1}MB)' -f $_.ProcessName, [math]::Round($_.WorkingSet64/1MB) }) -join ', '; "
            "Write-Output \"CPU:$cpu\"; "
            "Write-Output \"MEM:$usedMem/$totMem GB\"; "
            "Write-Output \"UP:$upStr\"; "
            "Write-Output \"TOP:$top\"; "
            "Write-Output \"WINS:$wins\""
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            kv: dict[str, str] = {}
            for ln in (proc.stdout or "").splitlines():
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    kv[k.strip()] = v.strip()
            if kv.get("CPU"):
                lines.append(f"- CPU 负载: {kv['CPU']}%")
            if kv.get("MEM"):
                lines.append(f"- 内存占用: {kv['MEM']}")
            if kv.get("UP"):
                lines.append(f"- 已开机时长: {kv['UP']}")
            if kv.get("TOP"):
                lines.append(f"- 内存 Top5: {kv['TOP']}")
            if kv.get("WINS"):
                lines.append(f"- 活跃窗口: {kv['WINS'][:220]}")
        except Exception as exc:
            lines.append(f"- 系统指标获取异常: {exc}")

        disk_parts: list[str] = []
        for drv in ("C:\\", "D:\\", "E:\\", "F:\\"):
            if Path(drv).exists():
                try:
                    du = shutil.disk_usage(drv)
                    free_gb = du.free / (1024**3)
                    disk_parts.append(f"{drv[0]}盘剩{free_gb:.0f}GB")
                except Exception:
                    pass
        if disk_parts:
            lines.append("- 磁盘余量: " + " | ".join(disk_parts))
    else:
        try:
            load1, load5, load15 = os.getloadavg()
            lines.append(f"- 系统负载 (1/5/15m): {load1:.2f} / {load5:.2f} / {load15:.2f}")
        except Exception:
            pass
        try:
            du = shutil.disk_usage(str(Path.home()))
            free_gb = du.free / (1024**3)
            tot_gb = du.total / (1024**3)
            lines.append(f"- 磁盘余量: 剩 {free_gb:.0f}GB / 共 {tot_gb:.0f}GB")
        except Exception:
            pass

    return "\n".join(lines)


# ── Daily Digest Formatting ───────────────────────────────────────────────────

_H3_LINK_RE = re.compile(r"^###\s+(\d+)\.\s+\[(.+?)\]\((https?://[^)]+)\)\s*$")
_H3_PLAIN_RE = re.compile(r"^###\s+(\d+)\.\s+(.+?)\s*$")


def find_latest_digest() -> Path | None:
    if not DIGEST_DIR.exists():
        return None
    files = sorted(
        DIGEST_DIR.glob("digest_*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def format_digest_for_wechat(markdown_text: str, filename: str = "") -> str:
    """Turn a Daily Digest Markdown into a crisp <=3200-char WeChat mobile digest."""
    lines = markdown_text.splitlines()
    title = ""
    sections: list[str] = []

    def _clean_md(s: str) -> str:
        s = re.sub(r"`([^`]+)`", r"\1", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
        s = re.sub(r"\*([^*]+)\*", r"\1", s)
        return s.strip()

    for raw in lines:
        s = raw.strip()
        if not title and s.startswith("# "):
            title = _clean_md(s[2:])
            continue
        if s.startswith("## "):
            hdr = _clean_md(s[3:])
            if "附录" in hdr:
                break
            sections.append(f"\n【{hdr}】")
            continue
        m = _H3_LINK_RE.match(s)
        if m:
            num, item_title, url = m.group(1), _clean_md(m.group(2)), m.group(3)
            sections.append(f"{num}. {item_title}\n   {url}")
            continue
        m2 = _H3_PLAIN_RE.match(s)
        if m2:
            num, item_title = m2.group(1), _clean_md(m2.group(2))
            sections.append(f"{num}. {item_title}")
            continue
        if s.startswith("- **💡 破圈看点**"):
            tip = _clean_md(s[2:])
            if len(tip) > 120:
                tip = tip[:118] + "…"
            sections.append(f"   {tip}")

    header = title or (f"每日精选日报 ({filename})" if filename else "Antigravity 每日精选日报")
    body = f"{header}\n" + "\n".join(sections)
    footer = "\n\n（发 /digest file 获取完整 Markdown 原文；发 /digest auto off 可关自动推送）"
    if len(body) + len(footer) > 3350:
        body = body[: 3350 - len(footer) - 2] + "…"
    return (body + footer).strip()
