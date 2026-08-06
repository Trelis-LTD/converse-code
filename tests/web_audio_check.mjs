/* Check the voice tab's audio wiring, and the vendored SDK's own audio math.

   History worth keeping: this page used to hand-roll decode/resample/playback,
   and shipped three defects in a row — PCM16 decoded as Float32 (pure noise), no
   jitter buffer, and clicks at chunk seams. Two of my own tests passed anyway,
   because they validated my assumptions against themselves. The audio path is
   now the official @trelis/converse SDK, so this checks:

     1. the page delegates to the SDK rather than reimplementing audio
     2. the SDK's decoder reads PCM16 — the wire format, verified against the
        live broker (an earlier npm release decoded f32 and produced noise)
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

// The SDK (>=0.5.0) decodes the downlink as PCM16, matching the broker's wire
// default and the output_encoding converse-code pins in the start frame — so
// frames pass through the Python side untouched. If a future SDK release changes
// this expectation, it fails here rather than becoming noise in someone's ears.
const pcm = new Int16Array([0, 16384, -16384, 32767, -32768]);
const decoded = await binaryToFloat32(pcm.buffer);
[0, 16384 / 32767, -0.5, 1, -1].forEach((want, i) => {
  if (Math.abs(decoded[i] - want) > 1e-4) {
    throw new Error(`SDK no longer decodes the downlink as PCM16 (index ${i}) — check audio.py`);
  }
});
// Uplink is PCM16 too, via the SDK's encoder, and round-trips.
const up = floatToPcm16Bytes(new Float32Array([0, 0.5, -0.5]));
if (up.byteLength !== 6) throw new Error(`uplink encoder should emit PCM16, got ${up.byteLength} bytes`);
const back = await binaryToFloat32(up.buffer ?? up);
if (Math.abs(back[1] - 0.5) > 1e-3) throw new Error("PCM16 round-trip is wrong");
console.log("SDK codec: PCM16 both directions: OK");

const rawPath = new URL("../tmp/tts_raw.bin", import.meta.url);
let stream;
if (existsSync(rawPath)) {
  // tts_raw.bin is untouched wire capture (PCM16) — exactly what the SDK's
  // decoder takes, so decode it the same way the page does.
  const raw = readFileSync(rawPath);
  stream = await binaryToFloat32(
    raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength - (raw.byteLength % 2)),
  );
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
