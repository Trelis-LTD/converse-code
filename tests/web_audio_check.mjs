/* Extract the page's audio math and verify it in isolation:
   - the mic worklet's cross-block resampler (continuity + rate accuracy)
   - the playback resampler's carry across chunks
   Run: node tests/web_audio_check.mjs (or via pytest) */
import { readFileSync } from "node:fs";

const html = readFileSync(new URL("../converse_code/web/index.html", import.meta.url), "utf8");

// 1. The whole inline script must parse.
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
new Function(script.replace(/document\.|window\./g, "globalThis.__unused_"));
console.log("inline script parses: OK");

// 2. Rebuild the worklet source the page ships and exercise the resampler.
const srcArray = script.match(/var WORKLET_SRC = \[([\s\S]*?)\]\.join/)[1];
const lines = [...srcArray.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) =>
  JSON.parse('"' + m[1] + '"'),
);
const workletSrc = lines.join("\n");

globalThis.sampleRate = 48000;
class AudioWorkletProcessor {}
globalThis.AudioWorkletProcessor = AudioWorkletProcessor;
globalThis.registerProcessor = () => {};
const Proc = new Function(
  "AudioWorkletProcessor",
  "sampleRate",
  "registerProcessor",
  workletSrc + "\nreturn MicCaptureProcessor;",
)(AudioWorkletProcessor, 48000, () => {});

const proc = new Proc({});
const sent = [];
proc.port = { postMessage: (m) => sent.push(m) };

// Feed 1 second of a 440 Hz sine in 128-sample blocks, as the browser would.
const BLOCK = 128,
  IN_RATE = 48000,
  SECONDS = 1;
let phase = 0;
for (let b = 0; b < (IN_RATE * SECONDS) / BLOCK; b++) {
  const block = new Float32Array(BLOCK);
  for (let i = 0; i < BLOCK; i++, phase++) {
    block[i] = Math.sin((2 * Math.PI * 440 * phase) / IN_RATE);
  }
  proc.process([[block]]);
}

const samplesSent = sent.length * 640;
console.log(`frames sent: ${sent.length} (${samplesSent} samples @16k for 1s of input)`);
if (Math.abs(samplesSent - 16000) > 640) {
  throw new Error(`rate drift: expected ~16000 samples, got ${samplesSent}`);
}

// Reassemble and check for discontinuities at block boundaries. A clean 440 Hz
// resample has a bounded max step between consecutive samples.
const all = new Float32Array(samplesSent);
sent.forEach((m, i) => {
  const view = new Int16Array(m.pcm);
  for (let j = 0; j < view.length; j++) all[i * 640 + j] = view[j] / 0x7fff;
});
let maxStep = 0;
for (let i = 1; i < all.length; i++) {
  maxStep = Math.max(maxStep, Math.abs(all[i] - all[i - 1]));
}
const expectedStep = 2 * Math.PI * (440 / 16000); // ~0.173
console.log(`max sample-to-sample step: ${maxStep.toFixed(4)} (clean ≈ ${expectedStep.toFixed(4)})`);
if (maxStep > expectedStep * 2) {
  throw new Error(`discontinuity at chunk boundary — max step ${maxStep}`);
}
console.log("mic resampler: continuous, no drift: OK");

// 3. Playback resampler: 16k stream -> 48k device, chunk by chunk. The carry
// sample is what keeps chunk boundaries continuous.
const fnSrc = script.match(/function resampleToCtxRate\(input\)\{[\s\S]*?\n  \}/)[0];
const makeResampler = new Function(
  "ratio",
  `let resampleRatio = ratio, resampleCarry = 0; ${fnSrc}; return resampleToCtxRate;`,
);
const resample = makeResampler(3);

const CHUNK = 320; // 20ms @16k, a plausible TTS frame
let outChunks = [];
phase = 0;
for (let c = 0; c < 50; c++) {
  const chunk = new Float32Array(CHUNK);
  for (let i = 0; i < CHUNK; i++, phase++) {
    chunk[i] = Math.sin((2 * Math.PI * 440 * phase) / 16000);
  }
  outChunks.push(resample(chunk));
}
const up = new Float32Array(outChunks.reduce((n, c) => n + c.length, 0));
let off = 0;
for (const c of outChunks) {
  up.set(c, off);
  off += c.length;
}
let upMaxStep = 0;
for (let i = 1; i < up.length; i++) {
  upMaxStep = Math.max(upMaxStep, Math.abs(up[i] - up[i - 1]));
}
const upExpected = 2 * Math.PI * (440 / 48000); // ~0.0576
console.log(
  `playback: ${up.length} samples @48k from ${50 * CHUNK} @16k; ` +
    `max step ${upMaxStep.toFixed(4)} (clean ≈ ${upExpected.toFixed(4)})`,
);
if (upMaxStep > upExpected * 2) {
  throw new Error(`click at TTS chunk boundary — max step ${upMaxStep}`);
}
console.log("playback resampler: continuous across chunks: OK");
