export const SAMPLE_RATE = 16000;
export const FRAME_SAMPLES = 512;
export const UPLINK_FORMAT_TAGGED = 'tagged-pcm16-v1';
export const UPLINK_CHANNEL_PROCESSED = 0;
export const UPLINK_CHANNEL_RAW = 1;
const UPLINK_HEADER_BYTES = 12;

export function floatToPcm16Bytes(audio) {
  if (audio instanceof Uint8Array) return audio;
  if (audio instanceof Int16Array) return new Uint8Array(audio.buffer, audio.byteOffset, audio.byteLength);
  const pcm = new Int16Array(audio.length);
  for (let i = 0; i < audio.length; i += 1) {
    const s = Math.max(-1, Math.min(1, audio[i]));
    pcm[i] = s < 0 ? s * 32768 : s * 32767;
  }
  return new Uint8Array(pcm.buffer);
}

// Negotiated v1 uplink: "VL", version, channel, uint32 sequence, uint32 capture clock ms,
// then PCM16. The capture clock is performance.now() on the page's shared monotonic timeline,
// so independently-opened desktop streams can still be correlated server-side.
export function encodeTaggedPcm16(audio, { channel, sequence, captureMs }) {
  if (channel !== UPLINK_CHANNEL_PROCESSED && channel !== UPLINK_CHANNEL_RAW) {
    throw new RangeError('unknown uplink channel');
  }
  const pcm = floatToPcm16Bytes(audio);
  const out = new Uint8Array(UPLINK_HEADER_BYTES + pcm.byteLength);
  out[0] = 0x56; out[1] = 0x4c; out[2] = 1; out[3] = channel;
  const view = new DataView(out.buffer);
  view.setUint32(4, sequence >>> 0, true);
  view.setUint32(8, Math.max(0, Math.round(captureMs || 0)) >>> 0, true);
  out.set(pcm, UPLINK_HEADER_BYTES);
  return out;
}

// Base64-encode bytes in chunks (String.fromCharCode.apply caps out on large arrays). Used for
// the optional raw-mic ablation track, which rides a JSON control rather than a binary frame.
export function bytesToBase64(bytes) {
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}

export async function binaryToFloat32(data) {
  let bytes;
  if (data instanceof ArrayBuffer) bytes = new Uint8Array(data);
  else if (data instanceof Uint8Array) bytes = data;
  else if (ArrayBuffer.isView(data)) bytes = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  else if (data instanceof Blob) bytes = new Uint8Array(await data.arrayBuffer());
  else throw new TypeError('unsupported binary audio payload');
  if (bytes.byteLength % 4 !== 0) throw new Error('pcm_f32le payload must be divisible by 4');
  return new Float32Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
}

export function toWebSocketUrl(url) {
  const u = new URL(url);
  if (u.protocol === 'https:') u.protocol = 'wss:';
  if (u.protocol === 'http:') u.protocol = 'ws:';
  return u.toString();
}

export function createSessionId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}
