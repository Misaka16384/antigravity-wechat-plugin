# Antigravity WeChat Assistant Plugin (`wechat-assistant`)

All-in-one Antigravity plugin that connects your **WeChat** to **Antigravity App**:

- **Official WeChat ClawBot (iLink) Bridge Sidecar**: Send text, images, PDFs, documents, and videos from mobile WeChat directly to Antigravity, with automatic reply pushback via `Stop` Hook.
- **Native Multimodal Voice Listening (Zero-Compiler Cross-Platform)**: Downloads WeChat voice messages and decodes `SILK v3` -> standard 24 kHz `.wav` via **`silk-wasm`** (pure WebAssembly, zero C compiler needed on Windows, macOS Apple Silicon/Intel, or Linux, with `pilk` & `ffmpeg` fallbacks), so Antigravity directly hears your real voice, tone, and intent via `read_file`—with automatic fallback to WeChat ASR text if decoding ever fails.
- **Built-in Proactive WeChat MCP Tools**: `wechat_assistant_status`, `wechat_send_file`, `wechat_send_image`, `wechat_send_text`—no hardcoded paths or manual shell approvals needed.
- **Integrated WeFlow MCP Proxy**: Automatically discovers `weflow-cli`, fixes Antigravity's tool-name compatibility (`wechat.list_sessions` -> `wechat_list_sessions` so all 32 tools pass `^[a-zA-Z0-9_-]{1,64}$`), and auto-installs `weflow-cli` during `/wechat-setup` if missing.
- **Power-User WeChat Slash Commands**: `/ls`, `/bind`, `/unbind`, `/projects`, `/new-in`, `/watch`, `/unwatch`, `/screen`, `/sys`, `/model`, `/status`, `/help`.

### Platform Compatibility

| Capability | Windows | macOS (Apple Silicon & Intel) | Linux |
| :--- | :---: | :---: | :---: |
| WeChat ClawBot (iLink) Bridge + Stop Hook | ✅ | ✅ | ✅ |
| Native Multimodal Voice (`silk-wasm` WASM) | ✅ | ✅ | ✅ |
| Proactive `wechat_send_*` MCP Tools | ✅ | ✅ | ✅ |
| Slash Commands (`/ls`, `/bind`, `/watch`, `/sys`, `/screen`*) | ✅ | ✅ | ✅ |
| WeFlow Local WeChat DB Search (`weflow-cli`) | ✅ | ⚠️ Experimental (SIP / manual passphrase) | ✅ |

*\*On macOS, `/screen` uses native `screencapture` and requires granting Screen Recording permission once in System Settings.*

---

## Quick Start (One-Click Install + Guided Onboarding)

### Prerequisites
- **Antigravity App** (required for background Sidecar support)
- **Node.js >= 22.13** (for MCP server & `weflow-cli`)
- **Python 3.10+** (used to create an isolated plugin venv; does not pollute system Python)

### Step 1: Install the Plugin

**Option A — CLI install (Windows / macOS / Linux):**

```bash
git clone https://github.com/Misaka16384/antigravity-wechat-plugin.git
agy plugin install ./antigravity-wechat-plugin
```

**Option B — Convenience install + interactive setup script:**

- Windows (PowerShell):
  ```powershell
  git clone https://github.com/Misaka16384/antigravity-wechat-plugin.git
  cd antigravity-wechat-plugin
  .\scripts\install.ps1
  ```
- macOS / Linux:
  ```bash
  git clone https://github.com/Misaka16384/antigravity-wechat-plugin.git
  cd antigravity-wechat-plugin
  bash ./scripts/install.sh
  ```

### Step 2: Run Guided First-Run Onboarding

Open Antigravity and type:

```text
/wechat-setup
```

Antigravity will inspect your environment and walk you step by step through:
1. Creating an isolated Python venv and installing `requests`, `cryptography`, `pilk` (SILK v3 native voice decoder), `qrcode`, and `Pillow`.
2. Automatically installing `weflow-cli` if not already installed.
3. Scanning the **WeChat ClawBot (iLink)** QR code on your phone.
4. Optionally running `weflow-cli init` to connect your local PC WeChat database for chat/favorites search.
5. Enabling the `wechat-assistant/bridge` Sidecar and verifying with a live text + voice message.

*(You can also run the exact same wizard directly in any terminal via `node scripts/run-py.mjs sidecars/bridge/setup.py --all`.)*

---

## Architecture & Directory Layout

```text
antigravity-wechat-plugin/
├── plugin.json                      # Plugin manifest
├── mcp_config.json                  # Unified MCP server (`wx`) using ${PLUGIN_ROOT}
├── hooks.json                       # Cross-platform fast-path Stop hook
├── assets/logo.svg                  # Plugin icon
├── rules/AGENTS.md                  # Scoped WeChat formatting, voice & tool rules
├── skills/wechat-setup/SKILL.md     # /wechat-setup guided onboarding skill
├── scripts/
│   ├── wechat-mcp-server.mjs        # Built-in send/status tools + WeFlow MCP proxy
│   ├── silk-decode.mjs              # Pure-WASM SILK v3 -> WAV decoder (silk-wasm)
│   ├── run-py.mjs                   # Cross-platform venv-first Python launcher + macOS PATH
│   ├── run-stop-hook.mjs            # Fast-path Stop hook wrapper (~5ms for non-WeChat turns)
│   ├── install.ps1                  # Windows one-step install + wizard helper
│   └── install.sh                   # macOS/Linux one-step install + wizard helper
└── sidecars/bridge/
    ├── sidecar.json                 # Plugin Sidecar (`wechat-assistant/bridge`)
    ├── bridge.py                    # iLink long-poll daemon & slash commands
    ├── ilink_client.py              # iLink API + AES-128-ECB CDN + SILK v3 -> WAV decoder
    ├── stop_hook.py                 # Pushes final replies & /watch alerts back to WeChat
    ├── send_reply.py                # Outbound text/image/file CLI helper
    ├── login.py                     # Interactive ClawBot QR login
    ├── extras.py                    # Projects, screenshots, system stats, digest helpers
    ├── setup.py                     # Guided setup & health-check CLI
    ├── config_paths.py              # Unified PLUGIN_DATA storage (zero hardcoded paths)
    └── requirements.txt             # Isolated venv dependencies
```

---

## Data & Privacy

- All runtime credentials (`account.json`), session state (`state.json`), and the isolated Python venv live outside the plugin directory under `~/.gemini/antigravity/plugin_data/wechat-assistant/`.
- Downloaded WeChat attachments & decoded voice `.wav` files are saved to `~/WeChatInbox/YYYY-MM-DD/` by default (customizable via `setup.py --inbox <path>`).
- WeFlow local WeChat database access is completely read-only, local-only, and optional.
