#!/usr/bin/env node
/**
 * Cross-platform pure-WebAssembly SILK v3 -> WAV decoder powered by `silk-wasm`.
 * Works identically on Windows (x64/arm64), macOS (Apple Silicon / Intel), and Linux
 * without any C compiler or Python version restrictions.
 *
 * Usage: node silk-decode.mjs <input.silk> <output.wav> [sampleRate=24000]
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { resolveDataDir } from './run-py.mjs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PLUGIN_ROOT = path.resolve(__dirname, '..');

function findSilkWasmModule() {
  const dataDir = resolveDataDir();
  const candidates = [
    process.env.WECHAT_SILK_WASM_ENTRY,
    path.join(dataDir, 'silk', 'node_modules', 'silk-wasm', 'lib', 'index.mjs'),
    path.join(PLUGIN_ROOT, 'node_modules', 'silk-wasm', 'lib', 'index.mjs'),
  ];

  if (process.platform === 'win32' && process.env.APPDATA) {
    candidates.push(
      path.join(process.env.APPDATA, 'npm', 'node_modules', 'silk-wasm', 'lib', 'index.mjs')
    );
  } else {
    candidates.push(
      '/opt/homebrew/lib/node_modules/silk-wasm/lib/index.mjs',
      '/usr/local/lib/node_modules/silk-wasm/lib/index.mjs',
      path.join(os.homedir(), '.npm-global', 'lib', 'node_modules', 'silk-wasm', 'lib', 'index.mjs')
    );
  }

  for (const p of candidates) {
    if (p && fs.existsSync(p)) {
      return p;
    }
  }
  return null;
}

function buildWavBuffer(pcmBytes, sampleRate, numChannels = 1, bitsPerSample = 16) {
  const byteRate = (sampleRate * numChannels * bitsPerSample) / 8;
  const blockAlign = (numChannels * bitsPerSample) / 8;
  const dataSize = pcmBytes.byteLength;
  const header = Buffer.alloc(44);

  header.write('RIFF', 0);
  header.writeUInt32LE(36 + dataSize, 4);
  header.write('WAVE', 8);
  header.write('fmt ', 12);
  header.writeUInt32LE(16, 16); // PCM fmt chunk size
  header.writeUInt16LE(1, 20); // AudioFormat = 1 (PCM)
  header.writeUInt16LE(numChannels, 22);
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(byteRate, 28);
  header.writeUInt16LE(blockAlign, 32);
  header.writeUInt16LE(bitsPerSample, 34);
  header.write('data', 36);
  header.writeUInt32LE(dataSize, 40);

  return Buffer.concat([header, Buffer.from(pcmBytes.buffer, pcmBytes.byteOffset, pcmBytes.byteLength)]);
}

async function main() {
  const [inFile, outFile, rateArg] = process.argv.slice(2);
  if (!inFile || !outFile) {
    process.stderr.write('Usage: node silk-decode.mjs <input.silk> <output.wav> [sampleRate]\n');
    process.exit(2);
  }

  const modPath = findSilkWasmModule();
  if (!modPath) {
    process.stderr.write('silk-wasm not installed (run /wechat-setup or setup.py --install-deps)\n');
    process.exit(3);
  }

  const rate = Number(rateArg) || 24000;
  const validRates = new Set([8000, 12000, 16000, 24000, 32000, 44100, 48000]);
  const sampleRate = validRates.has(rate) ? rate : 24000;

  const { decode } = await import(pathToFileURL(modPath).href);
  const silkBytes = fs.readFileSync(inFile);
  const result = await decode(silkBytes, sampleRate);
  if (!result || !result.data || result.data.byteLength === 0) {
    process.stderr.write('silk-wasm decoded empty PCM output\n');
    process.exit(1);
  }

  const wavBuf = buildWavBuffer(result.data, sampleRate, 1, 16);
  fs.mkdirSync(path.dirname(path.resolve(outFile)), { recursive: true });
  fs.writeFileSync(outFile, wavBuf);
}

main().catch((err) => {
  process.stderr.write(`silk-decode failed: ${err?.message || err}\n`);
  process.exit(1);
});
