#!/usr/bin/env node
/**
 * Fast-path cross-platform Stop hook wrapper for WeChat Assistant.
 *
 * For non-WeChat desktop conversations (~99% of Stop events), exits immediately
 * in pure Node (~5ms) without spawning Python. Only spawns stop_hook.py when the
 * conversation is tracked/watched in state.json or starts with a WeChat prefix.
 */
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import { buildExtendedEnv, resolveDataDir, resolvePython } from './run-py.mjs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PLUGIN_ROOT = path.resolve(__dirname, '..');
const STOP_HOOK_PY = path.join(PLUGIN_ROOT, 'sidecars', 'bridge', 'stop_hook.py');

function readStdin() {
  try {
    return fs.readFileSync(0, 'utf8');
  } catch {
    return '';
  }
}

function shouldInvokePython(raw, dataDir) {
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    return false;
  }
  if (!payload || typeof payload !== 'object') return false;

  const convId = String(payload.conversationId || '').trim();
  if (!convId) return false;

  const lastInput = String(payload.lastUserInput || '').trim();
  if (lastInput.startsWith('[微信小助理消息')) {
    return true;
  }

  const stateFile = path.join(dataDir, 'state.json');
  if (!fs.existsSync(stateFile)) return false;
  try {
    const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
    if (state && typeof state === 'object') {
      if (state.tracked_conversations && convId in state.tracked_conversations) return true;
      if (state.watched_conversations && convId in state.watched_conversations) return true;
    }
  } catch {
    return true;
  }
  return false;
}

const raw = readStdin();
const dataDir = resolveDataDir();

if (!raw.trim() || !shouldInvokePython(raw, dataDir)) {
  process.stdout.write('{}\n');
  process.exit(0);
}

const pyExe = resolvePython();
const res = spawnSync(pyExe, [STOP_HOOK_PY], {
  input: raw,
  encoding: 'utf8',
  timeout: 28000,
  windowsHide: true,
  env: buildExtendedEnv({
    WECHAT_ASSISTANT_DATA_DIR: dataDir,
    PLUGIN_ROOT,
    PLUGIN_DATA: dataDir,
  }),
});

const out = (res.stdout || '').trim();
process.stdout.write((out || '{}') + '\n');
process.exit(0);
