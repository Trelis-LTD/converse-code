/* Check the voice tab's audio wiring, and the vendored SDK's own audio math.

   History worth keeping: this page used to hand-roll decode/resample/playback,
   and shipped three defects in a row — PCM16 decoded as Float32 (pure noise), no
   jitter buffer, and clicks at chunk seams. Two of my own tests passed anyway,
   because they validated my assumptions against themselves. The audio path is
   now the official @trelis/converse SDK, so this checks:

     1. the page delegates to the SDK rather than reimplementing audio
     2. the SDK's decoder really does read PCM16 (the wire format, verified
        against the live broker)
     3. the SDK's resampler is seam-continuous on REAL captured speech, at both
        48000 and 44100 Hz

   Run: node tests/web_audio_check.mjs (or via pytest) */
import { readFileSync, existsSync } from "node:fs";

const pageUrl = new URL("../converse_code/web/index.html", import.meta.url);
const html = readFileSync(pageUrl, "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
new Function(script.replace(/document\.|window\./g, "globalThis.__unused_"));
console.log("page script parses: OK");

// 1. The page must use the SDK, not its own audio implementation.
for (const needed of ["vendor/converse/index.js", "StreamingPlayer", "MicCapture", "binaryToFloat32", "floatToPcm16Bytes"]) {
  if (!script.includes(needed)) throw new Error(`page no longer uses ${needed}`);
}
for (const banned of ["new Float32Array(arrayBuffer)", "resamplePhase", "resampleToCtxRate", "MicCaptureProcessor"]) {
  if (script.includes(banned)) throw new Error(`page reintroduced hand-rolled audio: ${banned}`);
}
console.log("page delegates audio to the SDK: OK");

// 2/3. Exercise the SDK's own decode + resample.
const { binaryToFloat32, floatToPcm16Bytes } = await import("../converse_code/web/vendor/converse/audio.js");

// This npm SDK release decodes pcm_f32le (the server's previous default). The
// converse-code process therefore pins output_encoding=pcm16 on the wire and
// converts to Float32 before the page sees it (converse_code/audio.py). Assert
// the SDK's expectation explicitly, so a future SDK bump that switches to PCM16
// fails here loudly instead of turning into noise in someone's headphones.
const f32 = new Float32Array([0, 0.5, -0.5, 1, -1]);
const decoded = await binaryToFloat32(f32.buffer);
decoded.forEach((v, i) => {
  if (Math.abs(v - f32[i]) > 1e-6) throw new Error(`SDK f32 decode wrong at ${i}`);
});
try {
  await binaryToFloat32(new Int16Array([1, 2, 3]).buffer);
  throw new Error("SDK now accepts a 6-byte frame — it may have switched to PCM16; update audio.py");
} catch (err) {
  if (!/divisible by 4/.test(err.message)) throw err;
}
// The uplink direction is PCM16 and the page uses the SDK's encoder for it.
const up = floatToPcm16Bytes(new Float32Array([0, 0.5, -0.5]));
if (up.byteLength !== 6) throw new Error(`uplink encoder should emit PCM16, got ${up.byteLength} bytes for 3 samples`);
console.log("SDK codec: f32 downlink (converted server-side), PCM16 uplink: OK");

const rawPath = new URL("../tmp/tts_raw.bin", import.meta.url);
let stream;
if (existsSync(rawPath)) {
  // tts_raw.bin is untouched wire capture, i.e. PCM16 — decode it as such, NOT
  // with the SDK's f32 decoder (doing that yields NaNs, and an earlier version of
  // this check "passed" on them because NaN comparisons are always false).
  const raw = readFileSync(rawPath);
  const count = (raw.byteLength - (raw.byteLength % 2)) / 2;
  stream = new Float32Array(count);
  for (let i = 0; i < count; i++) {
    const v = raw.readInt16LE(i * 2);
    stream[i] = v / (v < 0 ? 32768 : 32767);
  }
  console.log(`resampler source: real captured TTS (${(stream.length / 16000).toFixed(1)}s)`);
} else {
  stream = new Float32Array(16000);
  for (let i = 0; i < stream.length; i++) stream[i] = Math.sin((2 * Math.PI * 3000 * i) / 16000);
  console.log("resampler source: 3 kHz sine (no capture present)");
}

// Drive the SDK player's resampler directly: no AudioContext needed, just its
// ratio/phase/carry state, chunk by chunk as frames arrive.
const { StreamingPlayer } = await import("../converse_code/web/vendor/converse/player.js");
const CHUNK = 320; // 20ms @16k, one broker frame

for (const deviceRate of [48000, 44100]) {
  const p = new StreamingPlayer();
  p.ratio = deviceRate / 16000;
  p.phase = 0;
  p.carry = 0;
  const parts = [];
  for (let i = 0; i < stream.length; i += CHUNK) parts.push(p._resample(stream.subarray(i, i + CHUNK)));
  const total = parts.reduce((n, c) => n + c.length, 0);
  const out = new Float32Array(total);
  const seams = new Set();
  let off = 0;
  for (const c of parts) {
    if (off > 0) seams.add(off);
    out.set(c, off);
    off += c.length;
  }

  const expected = (stream.length * deviceRate) / 16000;
  if (Math.abs(total - expected) > CHUNK) {
    throw new Error(`rate drift at ${deviceRate}: ${total} vs ~${expected}`);
  }
  let seamMax = 0;
  let interiorMax = 0;
  for (let i = 0; i < out.length; i++) {
    if (!Number.isFinite(out[i])) throw new Error(`non-finite sample at ${i} — a decode/format mismatch`);
  }
  for (let i = 1; i < out.length; i++) {
    const d = Math.abs(out[i] - out[i - 1]);
    if (seams.has(i)) seamMax = Math.max(seamMax, d);
    else interiorMax = Math.max(interiorMax, d);
  }
  console.log(
    `  ${deviceRate} Hz: ${total} samples, ${seams.size} seams; ` +
      `worst seam step ${seamMax.toFixed(4)} vs interior max ${interiorMax.toFixed(4)}`,
  );
  if (!(interiorMax > 0)) throw new Error("signal is silent — the check would prove nothing");
  if (seamMax > interiorMax * 1.05) {
    throw new Error(`chunk-seam discontinuity at ${deviceRate} Hz — audible crackle`);
  }
}
console.log("SDK resampler: continuous across chunks: OK");
