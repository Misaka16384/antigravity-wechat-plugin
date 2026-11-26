#!/usr/bin/env node
/**
 * Cross-platform Python launcher for the WeChat Assistant plugin.
 * - Prefers the plugin's private venv inside PLUGIN_DATA so dependencies
 *   never pollute system Python.
 * - Augments PATH on macOS/Linux so GUI-launched Sidecars & Hooks can locate
 *   Homebrew, nvm, Volta, pyenv, and ~/.gemini/antigravity/bin executables.
 */
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PLUGIN_ROOT = path.resolve(__dirname, '..');
const PLUGIN_ID = path.basename(PLUGIN_ROOT) || 'wechat-assistant';

export function resolveDataDir() {
  return (
    process.env.WECHAT_ASSISTANT_DATA_DIR ||
    process.env.PLUGIN_DATA ||
    path.join(os.homedir(), '.gemini', 'antigravity', 'plugin_data', PLUGIN_ID)
  );
}

export function buildExtendedEnv(extra = {}) {
  const home = os.homedir();
  const nodeDir = path.dirname(process.execPath);
  const sep = process.platform === 'win32' ? ';' : ':';
  const curPath = process.env.PATH || '';
  const extraDirs =
    process.platform === 'win32'
      ? [nodeDir, path.join(home, '.gemini', 'antigravity', 'bin')]
      : [
          nodeDir,
          path.join(home, '.gemini', 'antigravity', 'bin'),
          '/opt/homebrew/bin',
          '/usr/local/bin',
          path.join(home, '.volta', 'bin'),
          path.join(home, '.pyenv', 'shims'),
          '/Library/Frameworks/Python.framework/Versions/Current/bin',
        ];

  const existingParts = new Set(curPath.split(sep).filter(Boolean));
  const merged = [...curPath.split(sep).filter(Boolean)];
  for (const d of extraDirs) {
    if (d && !existingParts.has(d) && fs.existsSync(d)) {
      merged.push(d);
      existingParts.add(d);
    }
  }

  return {
    ...process.env,
    PATH: merged.join(sep),
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    ...extra,
  };
}

function isRunnablePython(cmd, env) {
  try {
    const res = spawnSync(cmd, ['-c', 'import sys; print(sys.version_info[0])'], {
      encoding: 'utf8',
      timeout: 5000,
      windowsHide: true,
      env,
    });
    return res.status === 0 && (res.stdout || '').trim() === '3';
  } catch {
    return false;
  }
}

export function resolvePython() {
  const dataDir = resolveDataDir();
  const venvPy =
    process.platform === 'win32'
      ? path.join(dataDir, 'venv', 'Scripts', 'python.exe')
      : path.join(dataDir, 'venv', 'bin', 'python');

  if (fs.existsSync(venvPy)) {
    return venvPy;
  }

  const env = buildExtendedEnv();
  const home = os.homedir();
  const candidates = [
    process.env.WECHAT_ASSISTANT_PYTHON,
    ...(process.platform === 'win32'
      ? ['python', 'py']
      : [
          '/opt/homebrew/bin/python3',
          '/usr/local/bin/python3',
          '/Library/Frameworks/Python.framework/Versions/Current/bin/python3',
          path.join(home, '.pyenv', 'shims', 'python3'),
          'python3',
          'python',
        ]),
  ].filter(Boolean);

  for (const cand of candidates) {
    if (cand.includes(path.sep) && !fs.existsSync(cand)) {
      continue;
    }
    if (cand === 'py' && process.platform === 'win32') {
      try {
        const res = spawnSync('py', ['-3', '-c', 'import sys; print(sys.executable)'], {
          encoding: 'utf8',
          timeout: 5000,
          windowsHide: true,
          env,
        });
        const exe = (res.stdout || '').trim();
        if (res.status === 0 && exe && fs.existsSync(exe)) {
          return exe;
        }
      } catch {
        // ignore
      }
      continue;
    }
    if (isRunnablePython(cand, env)) {
      return cand;
    }
  }
  return process.platform === 'win32' ? 'python' : 'python3';
}

function resolveHeadlessWindowsPython(pyExe) {
  if (process.platform !== 'win32') return pyExe;
  const lower = pyExe.toLowerCase();
  if (lower.endsWith('python.exe')) {
    const pyw = pyExe.slice(0, -'python.exe'.length) + 'pythonw.exe';
    if (fs.existsSync(pyw)) return pyw;
  }
  return pyExe;
}

function main() {
  const pyArgs = process.argv.slice(2);
  if (!pyArgs.length) {
    process.stderr.write('Usage: node run-py.mjs [python-args...] <script.py> [args...]\n');
    process.exit(2);
  }

  const isInteractive = Boolean(process.stdin.isTTY || process.stdout.isTTY);
  const resolvedPy = resolvePython();
  const pyExe = isInteractive ? resolvedPy : resolveHeadlessWindowsPython(resolvedPy);
  const dataDir = resolveDataDir();
  const child = spawn(pyExe, pyArgs, {
    stdio: isInteractive ? 'inherit' : ['ignore', 'pipe', 'pipe'],
    windowsHide: !isInteractive,
    env: buildExtendedEnv({
      WECHAT_ASSISTANT_DATA_DIR: dataDir,
      PLUGIN_ROOT,
      PLUGIN_DATA: dataDir,
    }),
  });

  if (!isInteractive) {
    if (child.stdout) child.stdout.pipe(process.stdout);
    if (child.stderr) child.stderr.pipe(process.stderr);
  }

  child.on('error', (err) => {
    process.stderr.write(`[wechat-assistant] Failed to launch Python (${pyExe}): ${err.message}\n`);
    process.exit(1);
  });

  for (const sig of ['SIGINT', 'SIGTERM']) {
    process.on(sig, () => {
      if (!child.killed) child.kill(sig);
    });
  }

  child.on('exit', (code, signal) => {
    if (signal) process.exit(0);
    process.exit(code ?? 0);
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(__filename)) {
  main();
}
