---
name: wechat-setup
description: Interactive step-by-step onboarding wizard for the WeChat Assistant plugin across Windows, macOS, and Linux. Automatically creates the isolated Python venv, installs silk-wasm for zero-compiler native multimodal voice decoding, installs weflow-cli where supported, walks the user through WeChat ClawBot QR binding, and verifies the Sidecar & MCP connection.
---

# WeChat Assistant Guided Onboarding (`/wechat-setup`)

Follow this step-by-step interactive flow whenever the user runs `/wechat-setup` or asks to install, configure, log in, or troubleshoot WeChat Assistant, native voice decoding, or WeFlow.

**Core rule:** Guide the user **one interactive step at a time** (especially for QR scanning and WeFlow DB init), never skip verification, and adapt honestly to the user's OS (`platform`: `windows`, `macos`, or `linux`).

## Step 1: Inspect Current Status

Call the built-in MCP tool `wechat_assistant_status` first. If unavailable, run:

```bash
node <PLUGIN_ROOT>/scripts/run-py.mjs <PLUGIN_ROOT>/sidecars/bridge/setup.py --check --json
```

Inspect the returned JSON fields:
- `platform` (`windows` | `macos` | `linux`)
- `python_deps_ready` (`requests`, `cryptography`, `qrcode`, `PIL`)
- `voice_decoder_ready` (`silk_wasm_installed` or `pilk_installed`)
- `weflow_installed` (`weflow-cli` MCP server found)
- `wechat_logged_in` (`account.json` present)
- `weflow_db_configured` (local WeChat DB configured in `weflow-cli`)
- `sidecar_enabled` (`wechat-assistant/bridge` enabled)

Summarize what is already ready and what remains, then proceed step by step.

## Step 2: Install Isolated Python Venv & `silk-wasm` Voice Decoder

If `python_deps_ready` or `voice_decoder_ready` is `false`:
1. Run:
   ```bash
   node <PLUGIN_ROOT>/scripts/run-py.mjs <PLUGIN_ROOT>/sidecars/bridge/setup.py --install-deps
   ```
2. This creates `<PLUGIN_DATA>/venv` (`requests`, `cryptography`, `qrcode[pil]`, `Pillow`) and installs `silk-wasm` into `<PLUGIN_DATA>/silk` (pure WebAssembly SILK v3 -> WAV decoder—works on Windows, macOS Apple Silicon/Intel, and Linux with zero C compilers and any Python 3.10+ version).
3. Verify `python_deps_ready` and `voice_decoder_ready` are both `true`.

## Step 3: Install `weflow-cli` (OS-Aware)

If `weflow_installed` is `false`:
- **On Windows / Linux**: Automatically install `weflow-cli`:
  ```bash
  node <PLUGIN_ROOT>/scripts/run-py.mjs <PLUGIN_ROOT>/sidecars/bridge/setup.py --install-weflow
  ```
- **On macOS**: Inform the user honestly that `weflow-cli` local WeChat database decryption is currently experimental on macOS (key capture requires `lldb` / macOS SIP handling or a manually supplied 64-char hex passphrase), and that **WeChat Assistant + native voice work completely without WeFlow**. Ask whether they still want to install `weflow-cli` now or skip directly to Step 4.
- Remind the user that after installing `weflow-cli`, refreshing MCP Servers (or restarting Antigravity) exposes all 32 `wechat_*` tools automatically.

## Step 4: WeChat ClawBot (iLink) QR Code Login

If `wechat_logged_in` is `false`:
1. Tell the user you are generating a WeChat ClawBot QR code and that they should scan and tap Confirm on their phone WeChat.
2. Run:
   ```bash
   node <PLUGIN_ROOT>/scripts/run-py.mjs <PLUGIN_ROOT>/sidecars/bridge/setup.py --login
   ```
   - Opens `<PLUGIN_DATA>/qrcode.png` automatically (`os.startfile` on Windows, `open` on macOS, `xdg-open` on Linux) and prints the path + URL while polling for confirmation.
   - If running headless, copy `<PLUGIN_DATA>/qrcode.png` into the conversation artifact directory and display it inline so the user can scan right from chat.
3. Wait for `[login] 绑定成功！凭证已保存。`.

## Step 5: Optional WeFlow Local WeChat Database Setup

If `weflow_installed` is `true` and `weflow_db_configured` is `false`:
1. Ask the user whether they want to connect **WeFlow** for local WeChat chat & favorites search.
2. On **Windows/Linux**, explain that PC WeChat should be open and logged in, then guide them to run `weflow-cli init` (on Linux, `weflow-cli capture-passphrase` first).
3. On **macOS**, explain the SIP / `weflow-cli fav set-key --passphrase <64-hex>` requirement cleanly and let them skip without blocking.

## Step 6: Enable Sidecar & Verify End-to-End

1. If `sidecar_enabled` is `false`, instruct the user to enable Sidecar `wechat-assistant/bridge` in Antigravity App (or run once in their own terminal):
   ```bash
   node <PLUGIN_ROOT>/scripts/run-py.mjs <PLUGIN_ROOT>/sidecars/bridge/setup.py --enable-sidecar
   ```
   Then restart Antigravity App (or refresh MCP Servers).
2. On **macOS**, mention that if they use `/screen`, macOS will prompt once to grant Screen Recording permission to Antigravity / Terminal.
3. Invite the user to test from phone WeChat:
   - Send `/status` to verify the bridge is online.
   - Send a short **voice message** to verify native multimodal audio listening (`silk-wasm` SILK v3 -> WAV -> `read_file`).
   - Ask to send a local file back to WeChat via `wechat_send_file`.
