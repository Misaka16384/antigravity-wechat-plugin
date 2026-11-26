"""Guided first-run onboarding & environment setup wizard for WeChat Assistant plugin.

Supports both interactive CLI onboarding (`python setup.py`) and step-by-step
automation flags for Antigravity's `/wechat-setup` skill:
  --check [--json]      Inspect current readiness across all components
  --install-deps        Create private plugin venv + install `silk-wasm` (zero-compiler voice decoder)
  --install-weflow      Ensure `weflow-cli` is installed (auto npm install if missing)
  --init-weflow         Run `weflow-cli init` interactively to bind local WeChat DB
  --login               Run WeChat ClawBot (iLink) QR login wizard
  --inbox <path>        Set custom local inbox directory for WeChat attachments
  --enable-sidecar      Enable `wechat-assistant/bridge` in ~/.gemini/config/config.json
  --all                 Run the full interactive guided setup step by step
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from typing import Any

from config_paths import (
    ACCOUNT_FILE,
    DATA_DIR,
    DEFAULT_INBOX_DIR,
    PLUGIN_DIR,
    QR_IMAGE_FILE,
    SIDECAR_DIR,
    SIDECAR_ID,
    SILK_LOCAL_DIR,
    VENV_DIR,
    WEFLOW_LOCAL_DIR,
    get_venv_python,
    load_account,
    load_state,
    locked_state,
)

CONFIG_FILE = Path.home() / ".gemini" / "config" / "config.json"
REQUIREMENTS_FILE = SIDECAR_DIR / "requirements.txt"
REQUIRED_PY_MODULES = ("requests", "cryptography", "qrcode", "PIL")


def _platform_label() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _find_bin(name: str) -> str | None:
    if os.name == "nt":
        found = shutil.which(name) or shutil.which(f"{name}.cmd") or shutil.which(f"{name}.exe")
        if found:
            return found
        return None
    found = shutil.which(name)
    if found:
        return found
    for d in (
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(Path.home() / ".volta" / "bin"),
        str(Path.home() / ".gemini" / "antigravity" / "bin"),
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


def _npm_cmd() -> str | None:
    return _find_bin("npm")


def find_silk_wasm_entry() -> Path | None:
    candidates: list[Path] = []
    if os.environ.get("WECHAT_SILK_WASM_ENTRY"):
        candidates.append(Path(os.environ["WECHAT_SILK_WASM_ENTRY"]))
    candidates.extend(
        [
            SILK_LOCAL_DIR / "node_modules" / "silk-wasm" / "lib" / "index.mjs",
            PLUGIN_DIR / "node_modules" / "silk-wasm" / "lib" / "index.mjs",
        ]
    )
    if os.name == "nt" and os.environ.get("APPDATA"):
        candidates.append(
            Path(os.environ["APPDATA"]) / "npm" / "node_modules" / "silk-wasm" / "lib" / "index.mjs"
        )
    else:
        candidates.extend(
            [
                Path("/opt/homebrew/lib/node_modules/silk-wasm/lib/index.mjs"),
                Path("/usr/local/lib/node_modules/silk-wasm/lib/index.mjs"),
                Path.home() / ".npm-global" / "lib" / "node_modules" / "silk-wasm" / "lib" / "index.mjs",
            ]
        )
    for c in candidates:
        if c.exists():
            return c
    return None


def find_weflow_entry() -> Path | None:
    candidates: list[Path] = []
    if os.environ.get("WEFLOW_MCP_ENTRY"):
        candidates.append(Path(os.environ["WEFLOW_MCP_ENTRY"]))
    candidates.append(WEFLOW_LOCAL_DIR / "node_modules" / "weflow-cli" / "dist" / "mcp-server" / "index.js")
    npm = _npm_cmd()
    if npm:
        try:
            proc = subprocess.run(
                [npm, "root", "-g"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=(os.name == "nt"),
            )
            groot = (proc.stdout or "").strip()
            if proc.returncode == 0 and groot:
                candidates.append(Path(groot) / "weflow-cli" / "dist" / "mcp-server" / "index.js")
        except Exception:
            pass
    if os.name == "nt" and os.environ.get("APPDATA"):
        candidates.append(
            Path(os.environ["APPDATA"]) / "npm" / "node_modules" / "weflow-cli" / "dist" / "mcp-server" / "index.js"
        )
    else:
        candidates.extend(
            [
                Path("/opt/homebrew/lib/node_modules/weflow-cli/dist/mcp-server/index.js"),
                Path("/usr/local/lib/node_modules/weflow-cli/dist/mcp-server/index.js"),
                Path.home() / ".npm-global" / "lib" / "node_modules" / "weflow-cli" / "dist" / "mcp-server" / "index.js",
            ]
        )
    for c in candidates:
        if c.exists():
            return c
    return None


def find_weflow_cli_cmd() -> list[str] | None:
    bin_path = _find_bin("weflow-cli")
    if bin_path:
        return [bin_path]
    local_cli = WEFLOW_LOCAL_DIR / "node_modules" / "weflow-cli" / "cli.cjs"
    node_bin = _find_bin("node") or "node"
    if local_cli.exists():
        return [node_bin, str(local_cli)]
    entry = find_weflow_entry()
    if entry is not None:
        cli_cjs = entry.parents[2] / "cli.cjs"
        if cli_cjs.exists():
            return [node_bin, str(cli_cjs)]
    return None


def check_python_deps(py_exe: Path) -> tuple[dict[str, bool], bool]:
    if not py_exe.exists():
        return ({m: False for m in REQUIRED_PY_MODULES}, False)
    code = (
        "import importlib.util, json; "
        f"mods = {list(REQUIRED_PY_MODULES)!r}; "
        "res = {m: bool(importlib.util.find_spec(m)) for m in mods}; "
        "print(json.dumps({'deps': res, 'pilk': bool(importlib.util.find_spec('pilk'))}))"
    )
    try:
        proc = subprocess.run(
            [str(py_exe), "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if proc.returncode == 0:
            parsed = json.loads(proc.stdout.strip())
            return parsed.get("deps", {}), bool(parsed.get("pilk"))
    except Exception:
        pass
    return ({m: False for m in REQUIRED_PY_MODULES}, False)


def check_weflow_db_configured() -> bool:
    cmd = find_weflow_cli_cmd()
    if not cmd:
        return False
    try:
        proc = subprocess.run(
            cmd + ["config", "show"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        out = proc.stdout or ""
        return "已设置" in out and ("NT 数据库" in out or "3.x 数据库" in out)
    except Exception:
        return False


def check_sidecar_enabled() -> bool:
    if not CONFIG_FILE.exists():
        return False
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        sidecars = cfg.get("sidecars") or {}
        entry = sidecars.get(SIDECAR_ID) or {}
        return bool(entry.get("enabled"))
    except Exception:
        return False


def collect_status() -> dict[str, Any]:
    plat = _platform_label()
    venv_py = get_venv_python()
    active_py = venv_py if venv_py.exists() else Path(sys.executable)
    py_deps, pilk_ok = check_python_deps(active_py)
    silk_entry = find_silk_wasm_entry()
    weflow_entry = find_weflow_entry()
    account = load_account()
    state = load_state()
    inbox_dir = state.get("inbox_dir") or str(DEFAULT_INBOX_DIR)
    sidecar_enabled = check_sidecar_enabled()
    weflow_configured = check_weflow_db_configured() if weflow_entry else False
    ffmpeg_ok = bool(_find_bin("ffmpeg"))
    voice_ready = (silk_entry is not None) or pilk_ok

    next_steps: list[str] = []
    if not all(py_deps.values()) or not voice_ready:
        next_steps.append("1) 安装插件独立 Python 依赖与 silk-wasm 原生语音解码器（--install-deps）")
    if not weflow_entry:
        if plat == "macos":
            next_steps.append("2) （可选，macOS 本地库解密受 SIP 限制为实验性支持）安装 weflow-cli（--install-weflow）")
        else:
            next_steps.append("2) 自动安装 weflow-cli（--install-weflow）")
    if not account:
        next_steps.append("3) 微信扫码绑定 ClawBot 账号（--login）")
    if weflow_entry and not weflow_configured:
        if plat == "macos":
            next_steps.append("4) （可选，macOS 实验性）参考 weflow-cli MACOS.md 配置本地微信口令并执行 weflow-cli init")
        else:
            next_steps.append("4) （可选）初始化 weflow 本地微信数据库（weflow-cli init）")
    if not sidecar_enabled:
        next_steps.append(f"5) 在 Antigravity 中启用 Sidecar `{SIDECAR_ID}`")

    return {
        "platform": plat,
        "plugin_root": str(PLUGIN_DIR),
        "data_dir": str(DATA_DIR),
        "sidecar_id": SIDECAR_ID,
        "inbox_dir": inbox_dir,
        "venv_exists": venv_py.exists(),
        "active_python": str(active_py),
        "python_deps": py_deps,
        "python_deps_ready": all(py_deps.values()),
        "silk_wasm_installed": silk_entry is not None,
        "silk_wasm_entry": str(silk_entry) if silk_entry else None,
        "pilk_installed": pilk_ok,
        "voice_decoder_ready": voice_ready,
        "ffmpeg_available": ffmpeg_ok,
        "weflow_installed": weflow_entry is not None,
        "weflow_mcp_entry": str(weflow_entry) if weflow_entry else None,
        "weflow_db_configured": weflow_configured,
        "wechat_logged_in": account is not None,
        "ilink_bot_id": (account or {}).get("ilink_bot_id", ""),
        "sidecar_enabled": sidecar_enabled,
        "ready": all(py_deps.values()) and voice_ready and (account is not None) and sidecar_enabled,
        "next_steps": next_steps,
    }


def install_silk_wasm() -> int:
    existing = find_silk_wasm_entry()
    if existing:
        print(f"  silk-wasm 原生语音解码器已就绪: {existing}", flush=True)
        return 0
    npm = _npm_cmd()
    if not npm:
        print("  未检测到 npm，跳过 silk-wasm 安装（请先安装 Node.js >= 22.13）。", file=sys.stderr, flush=True)
        return 1
    SILK_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  安装 silk-wasm（纯 WebAssembly，跨 Windows/macOS/Linux 零编译解码微信 SILK v3 语音）...", flush=True)
    proc = subprocess.run(
        [npm, "install", "--prefix", str(SILK_LOCAL_DIR), "silk-wasm", "--no-audit", "--no-fund"],
        shell=(os.name == "nt"),
    )
    entry = find_silk_wasm_entry()
    if proc.returncode == 0 and entry:
        print(f"  silk-wasm 安装成功: {entry}", flush=True)
        return 0
    print("  silk-wasm 安装失败，请检查网络或 npm 配置。", file=sys.stderr, flush=True)
    return 1


def install_python_deps() -> int:
    print(f"[Step 1/4] 准备插件独立 Python 虚拟环境与语音解码器: {VENV_DIR}", flush=True)
    venv_py = get_venv_python()
    if not venv_py.exists():
        VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
        venv.EnvBuilder(with_pip=True).create(str(VENV_DIR))
        print(f"  已创建虚拟环境: {venv_py}", flush=True)
    else:
        print(f"  虚拟环境已存在: {venv_py}", flush=True)

    cmd = [
        str(venv_py),
        "-m",
        "pip",
        "install",
        "-r",
        str(REQUIREMENTS_FILE),
        "--extra-index-url",
        "https://pypi.org/simple",
    ]
    print("  安装 Python 依赖（requests / cryptography / qrcode / Pillow）...", flush=True)
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        print("  Python 依赖安装失败，请检查网络或 Python 版本（推荐 Python 3.10+）。", file=sys.stderr, flush=True)
        return proc.returncode
    deps, pilk_ok = check_python_deps(venv_py)
    print(f"  Python 依赖校验结果: {deps}", flush=True)

    silk_rc = install_silk_wasm()
    voice_ok = (silk_rc == 0) or pilk_ok
    if not voice_ok:
        print("  警告：silk-wasm 安装未成功且未检测到 pilk，语音将暂时回退到微信转文字。", file=sys.stderr, flush=True)
    return 0 if all(deps.values()) and voice_ok else 1


def install_weflow() -> int:
    existing = find_weflow_entry()
    if existing:
        print(f"[Step 2/4] 检测到 weflow-cli 已安装: {existing}", flush=True)
        return 0

    npm = _npm_cmd()
    if not npm:
        print("[Step 2/4] 未检测到 npm，请先安装 Node.js (>= 22.13) 后再安装 weflow-cli。", file=sys.stderr, flush=True)
        return 1

    print("[Step 2/4] 未检测到 weflow-cli，正在自动安装（npm install -g weflow-cli）...", flush=True)
    proc = subprocess.run([npm, "install", "-g", "weflow-cli"], shell=(os.name == "nt"))
    if proc.returncode == 0 and find_weflow_entry():
        print(f"  全局安装成功: {find_weflow_entry()}", flush=True)
        return 0

    print("  全局 npm 安装未成功，自动回退到插件本地目录安装...", flush=True)
    WEFLOW_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    proc2 = subprocess.run(
        [npm, "install", "--prefix", str(WEFLOW_LOCAL_DIR), "weflow-cli"],
        shell=(os.name == "nt"),
    )
    entry = find_weflow_entry()
    if proc2.returncode == 0 and entry:
        print(f"  本地安装成功: {entry}", flush=True)
        return 0

    print("  weflow-cli 自动安装失败，可稍后手动执行: npm install -g weflow-cli", file=sys.stderr, flush=True)
    return 1


def run_qr_login(no_open: bool = False, copy_qr: str = "", timeout: int = 420) -> int:
    print("[Step 3/4] 启动微信 ClawBot (iLink) 扫码绑定...", flush=True)
    venv_py = get_venv_python()
    py_exe = str(venv_py) if venv_py.exists() else sys.executable
    cmd = [py_exe, str(SIDECAR_DIR / "login.py"), "--timeout", str(timeout)]
    if no_open:
        cmd.append("--no-open")
    if copy_qr:
        cmd.extend(["--copy-qr", copy_qr])
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "WECHAT_ASSISTANT_DATA_DIR": str(DATA_DIR),
        "PLUGIN_DATA": str(DATA_DIR),
    }
    proc = subprocess.run(cmd, env=env)
    return proc.returncode


def run_weflow_init() -> int:
    cmd = find_weflow_cli_cmd()
    if not cmd:
        print("未找到 weflow-cli，请先执行 --install-weflow。", file=sys.stderr, flush=True)
        return 1
    if sys.platform == "darwin":
        print(
            "[Step 4/4] 提示：macOS 上 weflow-cli 捕获微信 4.x 口令受 SIP/Hardened Runtime 限制，"
            "目前属实验性功能（如需自动捕获请先查阅 weflow-cli 的 MACOS.md，或手动设置 64 位 hex 口令）。",
            flush=True,
        )
    else:
        print("[Step 4/4] 启动 weflow-cli init 自动检测本地微信数据目录与密钥...", flush=True)
    proc = subprocess.run(cmd + ["init"])
    return proc.returncode


def set_inbox_dir(path_str: str) -> int:
    p = Path(path_str).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    with locked_state() as st:
        st["inbox_dir"] = str(p)
    print(f"已设置微信文件收件箱目录: {p}", flush=True)
    return 0


def enable_sidecar_in_config(project_id: str = "") -> int:
    """Enable the plugin sidecar in ~/.gemini/config/config.json per docs/sidecars.md."""
    if not CONFIG_FILE.exists():
        print(f"未找到 {CONFIG_FILE}，请先至少启动一次 Antigravity App。", file=sys.stderr, flush=True)
        return 1
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    sidecars = cfg.setdefault("sidecars", {})
    entry = sidecars.setdefault(SIDECAR_ID, {"schemaVersion": 1})
    entry["enabled"] = True
    if project_id:
        entry["projectId"] = project_id

    inbox_dir = Path((load_state().get("inbox_dir") or str(DEFAULT_INBOX_DIR)))
    inbox_dir.mkdir(parents=True, exist_ok=True)

    tmp = CONFIG_FILE.with_suffix(f".json.{os.getpid()}.tmp")
    raw = json.dumps(cfg, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_FILE)
    print(f"已在 config.json 中启用 Sidecar `{SIDECAR_ID}`（收件箱: {inbox_dir}）。", flush=True)
    return 0


def print_status_human(st: dict[str, Any]) -> None:
    ok = lambda b: "OK" if b else "MISSING"
    print(f"=== WeChat Assistant 插件状态体检 ({st['platform']}) ===")
    print(f"插件目录:        {st['plugin_root']}")
    print(f"数据目录:        {st['data_dir']}")
    print(f"Sidecar ID:      {st['sidecar_id']} [{ok(st['sidecar_enabled'])}]")
    print(f"收件箱目录:      {st['inbox_dir']}")
    print(f"Python 独立环境: {st['active_python']} [{ok(st['venv_exists'])}]")
    print(f"Python 依赖:     {st['python_deps']} [{ok(st['python_deps_ready'])}]")
    print(f"原生语音解码:    silk-wasm={st['silk_wasm_installed']} pilk={st['pilk_installed']} [{ok(st['voice_decoder_ready'])}]")
    print(f"ffmpeg 兜底:     [{ok(st['ffmpeg_available'])}]")
    print(f"weflow-cli:      {st['weflow_mcp_entry'] or '未安装'} [{ok(st['weflow_installed'])}]")
    print(f"weflow 本地库:   [{ok(st['weflow_db_configured'])}]")
    print(f"微信 ClawBot:    {st['ilink_bot_id'] or '未扫码绑定'} [{ok(st['wechat_logged_in'])}]")
    if st["next_steps"]:
        print("\n待完成步骤:")
        for step in st["next_steps"]:
            print(f"  - {step}")
    else:
        print("\n全部就绪！可以直接在手机微信给 ClawBot 发文字或语音测试。")


def run_interactive_all(no_open: bool = False) -> int:
    plat = _platform_label()
    print(f"🐾 欢迎使用 Antigravity WeChat Assistant 首次配置向导 ({plat})！\n", flush=True)
    inbox = Path(load_state().get("inbox_dir") or str(DEFAULT_INBOX_DIR))
    inbox.mkdir(parents=True, exist_ok=True)

    rc = install_python_deps()
    if rc != 0:
        return rc

    if plat == "macos":
        if not find_weflow_entry():
            try:
                ans_wf = input(
                    "\n[Step 2/4] 检测到 macOS：weflow-cli 的本地微信库解密受 SIP 限制目前为实验性支持。\n"
                    "是否仍要安装 weflow-cli（微信小助理与原生语音不依赖它）？(y/N): "
                ).strip().lower()
            except EOFError:
                ans_wf = "n"
            if ans_wf in ("y", "yes"):
                install_weflow()
        else:
            print(f"[Step 2/4] 检测到 weflow-cli 已安装: {find_weflow_entry()}", flush=True)
    else:
        install_weflow()

    if not load_account():
        rc_login = run_qr_login(no_open=no_open)
        if rc_login != 0:
            return rc_login
    else:
        print(f"[Step 3/4] 已检测到微信 ClawBot 登录凭证 ({ACCOUNT_FILE})，跳过扫码。", flush=True)

    if find_weflow_entry() and not check_weflow_db_configured():
        default_prompt = "(y/N)" if plat == "macos" else "(Y/n)"
        try:
            ans = input(
                f"\n[Step 4/4] 是否现在运行 `weflow-cli init` 绑定本机微信数据库以启用聊天/收藏搜索？{default_prompt}: "
            ).strip().lower()
        except EOFError:
            ans = "n"
        want_init = ans in ("y", "yes") if plat == "macos" else ans in ("", "y", "yes")
        if want_init:
            run_weflow_init()
    elif check_weflow_db_configured():
        print("[Step 4/4] weflow 本地微信数据库已配置完成。", flush=True)

    if not check_sidecar_enabled():
        try:
            ans = input(f"\n是否现在自动在 ~/.gemini/config/config.json 中启用 Sidecar `{SIDECAR_ID}`？(Y/n): ").strip().lower()
        except EOFError:
            ans = "n"
        if ans in ("", "y", "yes"):
            enable_sidecar_in_config()

    print("\n配置完成！如 Antigravity 已打开，请刷新 MCP Servers 或重启一次 Antigravity App 让 Sidecar 与工具全部生效。", flush=True)
    print(f"二维码路径备查: {QR_IMAGE_FILE}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="WeChat Assistant Plugin Setup Wizard")
    parser.add_argument("--check", action="store_true", help="Inspect setup status")
    parser.add_argument("--json", action="store_true", help="Output status as JSON (with --check)")
    parser.add_argument("--install-deps", action="store_true", help="Create venv, install Python deps & silk-wasm")
    parser.add_argument("--install-weflow", action="store_true", help="Install weflow-cli automatically if missing")
    parser.add_argument("--init-weflow", action="store_true", help="Run weflow-cli init")
    parser.add_argument("--login", action="store_true", help="Run WeChat ClawBot QR login")
    parser.add_argument("--no-open", action="store_true", help="Do not auto-open QR image window")
    parser.add_argument("--copy-qr", type=str, default="", help="Copy generated QR PNG to extra path")
    parser.add_argument("--timeout", type=int, default=420, help="QR login timeout in seconds")
    parser.add_argument("--inbox", type=str, default="", help="Set custom inbox directory")
    parser.add_argument("--enable-sidecar", action="store_true", help="Enable plugin sidecar in config.json")
    parser.add_argument("--project-id", type=str, default="", help="Optional projectId when enabling sidecar")
    parser.add_argument("--all", action="store_true", help="Run full guided onboarding wizard")
    args = parser.parse_args()

    if args.check:
        st = collect_status()
        if args.json:
            print(json.dumps(st, ensure_ascii=False, indent=2))
        else:
            print_status_human(st)
        return 0

    did_action = False
    if args.inbox:
        did_action = True
        set_inbox_dir(args.inbox)
    if args.install_deps:
        did_action = True
        rc = install_python_deps()
        if rc != 0:
            return rc
    if args.install_weflow:
        did_action = True
        rc = install_weflow()
        if rc != 0:
            return rc
    if args.login:
        did_action = True
        rc = run_qr_login(no_open=args.no_open, copy_qr=args.copy_qr, timeout=args.timeout)
        if rc != 0:
            return rc
    if args.init_weflow:
        did_action = True
        rc = run_weflow_init()
        if rc != 0:
            return rc
    if args.enable_sidecar:
        did_action = True
        rc = enable_sidecar_in_config(project_id=args.project_id)
        if rc != 0:
            return rc
    if args.all or not did_action:
        return run_interactive_all(no_open=args.no_open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
