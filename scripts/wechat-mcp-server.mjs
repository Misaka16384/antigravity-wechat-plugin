#!/usr/bin/env node
/**
 * Unified zero-dependency MCP stdio server for the WeChat Assistant plugin.
 *
 * Provides:
 * 1. Built-in WeChat Assistant tools (always available):
 *    - wechat_assistant_status: inspect onboarding/runtime readiness
 *    - wechat_send_file: send any local file back to bound WeChat via iLink
 *    - wechat_send_image: send any local image back to bound WeChat via iLink
 *    - wechat_send_text: proactively send a text message back to bound WeChat
 * 2. Integrated WeFlow MCP proxy (auto-discovered, zero hardcoded paths):
 *    - Rewrites WeFlow's dotted tool names (`wechat.list_sessions` -> `wechat_list_sessions`)
 *      so they pass Antigravity's `^[a-zA-Z0-9_-]{1,64}$` validation and <= 64 char cap.
 *    - Maps calls back to original `wechat.*` names transparently.
 *    - Gracefully degrades when `weflow-cli` is not yet installed.
 */
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import { buildExtendedEnv, resolveDataDir, resolvePython } from './run-py.mjs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PLUGIN_ROOT = path.resolve(__dirname, '..');
const BRIDGE_DIR = path.join(PLUGIN_ROOT, 'sidecars', 'bridge');
const SEND_REPLY_PY = path.join(BRIDGE_DIR, 'send_reply.py');
const SETUP_PY = path.join(BRIDGE_DIR, 'setup.py');

// Antigravity prefixes plugin MCP tools as `mcp_<plugin>_<server>_<tool>` (<= 64 chars).
// For `mcp_wechat-assistant_wx_` (24 chars), keeping tool names <= 38 chars guarantees <= 62 chars.
const MAX_SAFE_LEN = 38;

const BUILTIN_TOOLS = [
  {
    name: 'wechat_assistant_status',
    description:
      'Check WeChat Assistant plugin setup and runtime status (Python venv, pilk native voice decoder, weflow-cli installation, ClawBot QR login, inbox folder, and Sidecar status).',
    inputSchema: {
      type: 'object',
      properties: {},
    },
  },
  {
    name: 'wechat_send_file',
    description:
      'Send a local file (PDF, document, archive, video, etc.) back to the user in WeChat via the bound ClawBot (iLink) channel.',
    inputSchema: {
      type: 'object',
      properties: {
        path: {
          type: 'string',
          description: 'Absolute path to the local file to send.',
        },
      },
      required: ['path'],
    },
  },
  {
    name: 'wechat_send_image',
    description:
      'Send a local image (PNG, JPG, WebP, GIF) back to the user in WeChat via the bound ClawBot (iLink) channel.',
    inputSchema: {
      type: 'object',
      properties: {
        path: {
          type: 'string',
          description: 'Absolute path to the local image file to send.',
        },
      },
      required: ['path'],
    },
  },
  {
    name: 'wechat_send_text',
    description:
      'Proactively send a text message to the user in WeChat via the bound ClawBot (iLink) channel.',
    inputSchema: {
      type: 'object',
      properties: {
        text: {
          type: 'string',
          description: 'Message text to send to WeChat.',
        },
      },
      required: ['text'],
    },
  },
];

const BUILTIN_NAMES = new Set(BUILTIN_TOOLS.map((t) => t.name));

function resolveNpmBin() {
  const sibling = path.join(
    path.dirname(process.execPath),
    process.platform === 'win32' ? 'npm.cmd' : 'npm'
  );
  if (fs.existsSync(sibling)) return sibling;
  if (process.platform !== 'win32') {
    for (const p of ['/opt/homebrew/bin/npm', '/usr/local/bin/npm']) {
      if (fs.existsSync(p)) return p;
    }
  }
  return process.platform === 'win32' ? 'npm.cmd' : 'npm';
}

function findWeflowMcpEntry() {
  const candidates = [];
  if (process.env.WEFLOW_MCP_ENTRY) {
    candidates.push(process.env.WEFLOW_MCP_ENTRY);
  }

  const dataDir = resolveDataDir();
  candidates.push(
    path.join(dataDir, 'weflow', 'node_modules', 'weflow-cli', 'dist', 'mcp-server', 'index.js')
  );

  // Query `npm root -g` dynamically across Windows / macOS / Linux
  try {
    const npmCmd = resolveNpmBin();
    const res = spawnSync(npmCmd, ['root', '-g'], {
      encoding: 'utf8',
      timeout: 5000,
      windowsHide: true,
      shell: process.platform === 'win32',
      env: buildExtendedEnv(),
    });
    const globalRoot = (res.stdout || '').trim();
    if (res.status === 0 && globalRoot) {
      candidates.push(path.join(globalRoot, 'weflow-cli', 'dist', 'mcp-server', 'index.js'));
    }
  } catch {
    // ignore
  }

  if (process.platform === 'win32' && process.env.APPDATA) {
    candidates.push(
      path.join(process.env.APPDATA, 'npm', 'node_modules', 'weflow-cli', 'dist', 'mcp-server', 'index.js')
    );
  } else {
    candidates.push(
      '/opt/homebrew/lib/node_modules/weflow-cli/dist/mcp-server/index.js',
      '/usr/local/lib/node_modules/weflow-cli/dist/mcp-server/index.js',
      path.join(os.homedir(), '.npm-global', 'lib', 'node_modules', 'weflow-cli', 'dist', 'mcp-server', 'index.js')
    );
  }

  for (const p of candidates) {
    if (p && fs.existsSync(p)) {
      return p;
    }
  }
  return null;
}

function sanitizeToolName(origName, used) {
  let base = String(origName).replace(/[^a-zA-Z0-9_-]/g, '_');
  if (!base) base = 'tool';
  if (base.length > MAX_SAFE_LEN) {
    base = base.slice(0, MAX_SAFE_LEN);
  }
  let candidate = base;
  let suffix = 2;
  while (used.has(candidate) || BUILTIN_NAMES.has(candidate)) {
    const tag = `_${suffix++}`;
    candidate = base.slice(0, MAX_SAFE_LEN - tag.length) + tag;
  }
  used.add(candidate);
  return candidate;
}

class WeflowBridge {
  constructor() {
    this.child = null;
    this.entryPath = null;
    this.readyPromise = null;
    this.nextId = 1;
    this.pending = new Map();
    this.safeToOriginal = new Map();
    this.cachedTools = null;
  }

  ensureStarted() {
    const entry = findWeflowMcpEntry();
    if (!entry) return Promise.resolve(false);

    if (this.child && !this.child.killed && this.entryPath === entry && this.readyPromise) {
      return this.readyPromise;
    }

    this.close();
    this.entryPath = entry;
    this.readyPromise = new Promise((resolve) => {
      const child = spawn(process.execPath, [entry], {
        stdio: ['pipe', 'pipe', 'inherit'],
        env: buildExtendedEnv(),
        windowsHide: true,
      });
      this.child = child;

      let buf = '';
      child.stdout.setEncoding('utf8');
      child.stdout.on('data', (chunk) => {
        buf += chunk;
        let idx;
        while ((idx = buf.indexOf('\n')) !== -1) {
          const line = buf.slice(0, idx).replace(/\r$/, '');
          buf = buf.slice(idx + 1);
          if (!line.trim()) continue;
          try {
            const msg = JSON.parse(line);
            if (msg.id !== undefined && this.pending.has(msg.id)) {
              const { resolve: resFn } = this.pending.get(msg.id);
              this.pending.delete(msg.id);
              resFn(msg);
            }
          } catch {
            // ignore non-JSON output
          }
        }
      });

      child.on('error', () => {
        this.child = null;
        this.readyPromise = null;
        resolve(false);
      });

      child.on('exit', () => {
        for (const [, { reject }] of this.pending) {
          reject(new Error('weflow MCP server exited unexpectedly'));
        }
        this.pending.clear();
        this.child = null;
        this.readyPromise = null;
      });

      child.stdin.on('error', () => {});

      this._request('initialize', {
        protocolVersion: '2024-11-05',
        capabilities: {},
        clientInfo: { name: 'wechat-assistant-plugin', version: '1.0.0' },
      })
        .then(() => {
          this._notify('notifications/initialized', {});
          resolve(true);
        })
        .catch(() => {
          this.close();
          resolve(false);
        });
    });

    return this.readyPromise;
  }

  _notify(method, params) {
    if (!this.child || this.child.stdin.destroyed) return;
    this.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method, params }) + '\n');
  }

  _request(method, params, timeoutMs = 45000) {
    return new Promise((resolve, reject) => {
      if (!this.child || this.child.stdin.destroyed) {
        reject(new Error('weflow MCP child not running'));
        return;
      }
      const id = this.nextId++;
      const timer = setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`weflow MCP timeout on ${method}`));
        }
      }, timeoutMs);

      this.pending.set(id, {
        resolve: (msg) => {
          clearTimeout(timer);
          resolve(msg);
        },
        reject: (err) => {
          clearTimeout(timer);
          reject(err);
        },
      });

      this.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  }

  async listTools() {
    const ok = await this.ensureStarted();
    if (!ok) {
      this.cachedTools = [];
      return [];
    }
    try {
      const resp = await this._request('tools/list', {}, 15000);
      const rawTools = resp?.result?.tools;
      if (!Array.isArray(rawTools)) return [];
      const used = new Set();
      this.safeToOriginal.clear();
      const rewritten = rawTools.map((tool) => {
        const safe = sanitizeToolName(tool.name, used);
        this.safeToOriginal.set(safe, tool.name);
        return { ...tool, name: safe };
      });
      this.cachedTools = rewritten;
      return rewritten;
    } catch {
      return [];
    }
  }

  async callTool(safeName, args) {
    const ok = await this.ensureStarted();
    if (!ok) {
      return {
        isError: true,
        content: [
          {
            type: 'text',
            text:
              'weflow-cli 尚未安装，无法调用本地微信查询工具。请运行 /wechat-setup（或执行 setup.py --install-weflow）自动安装并初始化 weflow-cli。',
          },
        ],
      };
    }
    if (this.safeToOriginal.size === 0) {
      await this.listTools();
    }
    let origName = this.safeToOriginal.get(safeName);
    if (!origName && safeName.startsWith('wechat_')) {
      origName = `wechat.${safeName.slice('wechat_'.length)}`;
    }
    try {
      const resp = await this._request(
        'tools/call',
        { name: origName || safeName, arguments: args || {} },
        60000
      );
      if (resp?.error) {
        return {
          isError: true,
          content: [{ type: 'text', text: resp.error.message || JSON.stringify(resp.error) }],
        };
      }
      return resp?.result || { content: [{ type: 'text', text: '' }] };
    } catch (err) {
      return {
        isError: true,
        content: [{ type: 'text', text: `weflow MCP 调用失败: ${err.message}` }],
      };
    }
  }

  close() {
    if (this.child && !this.child.killed) {
      try {
        this.child.kill();
      } catch {
        // ignore
      }
    }
    this.child = null;
    this.readyPromise = null;
  }
}

function runPythonScript(scriptPath, scriptArgs, timeoutMs = 45000) {
  const pyExe = resolvePython();
  const dataDir = resolveDataDir();
  const res = spawnSync(pyExe, [scriptPath, ...scriptArgs], {
    encoding: 'utf8',
    timeout: timeoutMs,
    windowsHide: true,
    env: buildExtendedEnv({
      WECHAT_ASSISTANT_DATA_DIR: dataDir,
      PLUGIN_ROOT,
      PLUGIN_DATA: dataDir,
    }),
  });
  const stdout = (res.stdout || '').trim();
  const stderr = (res.stderr || '').trim();
  if (res.error) {
    return { ok: false, text: `Python 执行失败 (${pyExe}): ${res.error.message}` };
  }
  if (res.status !== 0) {
    return { ok: false, text: stderr || stdout || `Exited with status ${res.status}` };
  }
  return { ok: true, text: stdout || 'OK' };
}

async function callBuiltinTool(name, args) {
  if (name === 'wechat_assistant_status') {
    const r = runPythonScript(SETUP_PY, ['--check', '--json'], 20000);
    return {
      isError: !r.ok,
      content: [{ type: 'text', text: r.text }],
    };
  }
  if (name === 'wechat_send_file') {
    const filePath = String(args?.path || '').trim();
    if (!filePath) {
      return { isError: true, content: [{ type: 'text', text: 'Missing required argument: path' }] };
    }
    const r = runPythonScript(SEND_REPLY_PY, ['--file', filePath], 60000);
    return { isError: !r.ok, content: [{ type: 'text', text: r.text }] };
  }
  if (name === 'wechat_send_image') {
    const imgPath = String(args?.path || '').trim();
    if (!imgPath) {
      return { isError: true, content: [{ type: 'text', text: 'Missing required argument: path' }] };
    }
    const r = runPythonScript(SEND_REPLY_PY, ['--image', imgPath], 60000);
    return { isError: !r.ok, content: [{ type: 'text', text: r.text }] };
  }
  if (name === 'wechat_send_text') {
    const text = String(args?.text || '').trim();
    if (!text) {
      return { isError: true, content: [{ type: 'text', text: 'Missing required argument: text' }] };
    }
    const r = runPythonScript(SEND_REPLY_PY, ['--text', text], 30000);
    return { isError: !r.ok, content: [{ type: 'text', text: r.text }] };
  }
  return { isError: true, content: [{ type: 'text', text: `Unknown tool: ${name}` }] };
}

const weflow = new WeflowBridge();

function sendJson(obj) {
  if (!process.stdout.destroyed && process.stdout.writable) {
    process.stdout.write(JSON.stringify(obj) + '\n');
  }
}

async function handleRequest(msg) {
  if (!msg || typeof msg !== 'object') return;
  const { id, method, params } = msg;

  // Notifications have no id
  if (id === undefined || id === null) {
    return;
  }

  try {
    if (method === 'initialize') {
      sendJson({
        jsonrpc: '2.0',
        id,
        result: {
          protocolVersion: params?.protocolVersion || '2024-11-05',
          capabilities: { tools: {} },
          serverInfo: { name: 'wechat-assistant-mcp', version: '1.0.0' },
        },
      });
      return;
    }

    if (method === 'ping') {
      sendJson({ jsonrpc: '2.0', id, result: {} });
      return;
    }

    if (method === 'tools/list') {
      const weflowTools = await weflow.listTools();
      sendJson({
        jsonrpc: '2.0',
        id,
        result: {
          tools: [...BUILTIN_TOOLS, ...weflowTools],
        },
      });
      return;
    }

    if (method === 'tools/call') {
      const toolName = String(params?.name || '');
      const toolArgs = params?.arguments || {};
      const result = BUILTIN_NAMES.has(toolName)
        ? await callBuiltinTool(toolName, toolArgs)
        : await weflow.callTool(toolName, toolArgs);
      sendJson({ jsonrpc: '2.0', id, result });
      return;
    }

    sendJson({
      jsonrpc: '2.0',
      id,
      error: { code: -32601, message: `Method not found: ${method}` },
    });
  } catch (err) {
    sendJson({
      jsonrpc: '2.0',
      id,
      error: { code: -32603, message: err?.message || String(err) },
    });
  }
}

let inBuf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  inBuf += chunk;
  let idx;
  while ((idx = inBuf.indexOf('\n')) !== -1) {
    const rawLine = inBuf.slice(0, idx).replace(/\r$/, '');
    inBuf = inBuf.slice(idx + 1);
    if (!rawLine.trim()) continue;
    try {
      const parsed = JSON.parse(rawLine);
      if (Array.isArray(parsed)) {
        for (const item of parsed) void handleRequest(item);
      } else {
        void handleRequest(parsed);
      }
    } catch {
      // ignore malformed input
    }
  }
});

process.stdin.on('end', () => {
  weflow.close();
  process.exit(0);
});

process.stdout.on('error', () => {});

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    weflow.close();
    process.exit(0);
  });
}
