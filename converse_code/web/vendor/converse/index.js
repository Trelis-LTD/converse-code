import {
  binaryToFloat32, bytesToBase64, createSessionId, encodeTaggedPcm16,
  floatToPcm16Bytes, SAMPLE_RATE, toWebSocketUrl,
  UPLINK_CHANNEL_PROCESSED, UPLINK_CHANNEL_RAW, UPLINK_FORMAT_TAGGED,
} from './audio.js';
import { StreamingPlayer } from './player.js';
import { EchoCanceller, needsSdkAec } from './aec.js';
import { MicCapture } from './mic.js';

export {
  FRAME_SAMPLES, SAMPLE_RATE, createSessionId, encodeTaggedPcm16, floatToPcm16Bytes,
  UPLINK_CHANNEL_PROCESSED, UPLINK_CHANNEL_RAW, UPLINK_FORMAT_TAGGED,
} from './audio.js';
export { StreamingPlayer } from './player.js';
export { EchoCanceller, needsSdkAec } from './aec.js';
export { MicCapture } from './mic.js';

// NO OUTPUT-ROUTING CODE, BY EXPERIMENT (2026-07-30): never touch navigator.audioSession —
// 'playback' breaks getUserMedia on real iPhones (a mic outage), and an 11-configuration
// on-device test showed modern iOS gives web pages no earpiece/speaker control at all (see
// StreamingPlayer's header + docs/user-feedback.md). A regression test pins this.

const LISTENING_WARMUP_FRAMES = 16; // 512 ms at 16 kHz/512-frame mic chunks; lets browser AEC adapt.
// Barge hard-clear fade (server sends `interrupted` with clear:true): long enough to read as a
// yield rather than a glitch, short enough that silence lands ~immediately vs the old drain.
const BARGE_CLEAR_FADE_S = 0.15;
const captureClockMs = () => {
  const monotonic = globalThis.performance?.now?.();
  return Number.isFinite(monotonic) ? monotonic : Date.now();
};

// The browser SDK is thin: stream mic frames up (via pushMicFrame), play assistant audio down, and
// reflect server events. Barge-in is SERVER-side — the broker runs the Silero reflex on the AEC'd
// mic uplink and yields the floor itself — so the client has no VAD/detection at all. This keeps
// every client (browser, native, phone) identical, and there's no onnxruntime-web to load.
//
// Server events: `turn` (reply starting), `asr`/`utterance` (transcripts), `done` (reply finished),
// `playback_pause`/`playback_resume` (reversible backchannel arbitration), `interrupted` (a final
// barge — the server stopped sending), and `canceled` (eager speculation retracted).
// One short-lived authorized control frame (feedback, client_error): open a fresh socket — the
// session's own socket is usually closed by the time these fire — send the frame, resolve on the
// server's ok. `kind` only labels the errors.
function sendOneShotFrame(kind, frame, { url, WebSocketImpl = globalThis.WebSocket, timeoutMs = 5000 }) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocketImpl(toWebSocketUrl(url));
    const timer = setTimeout(() => {
      try { ws.close(); } catch { /* noop */ }
      reject(new Error(`${kind} timed out`));
    }, timeoutMs);
    const settle = (fn, value) => { clearTimeout(timer); fn(value); };
    ws.addEventListener('open', () => { ws.send(JSON.stringify(frame)); }, { once: true });
    ws.addEventListener('message', (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (m.type === 'ok') settle(resolve, true);
        else settle(reject, new Error(m.detail || `${kind} rejected`));
      } catch (err) {
        settle(reject, err);
      }
      try { ws.close(1000); } catch { /* noop */ }
    }, { once: true });
    ws.addEventListener('error', () => settle(reject, new Error(`${kind} socket failed`)), { once: true });
    // A clean close without a reply (proxy cut the socket, server died between accept and ack)
    // must fail fast, not wait out timeoutMs. settle() on an already-settled promise is a no-op.
    ws.addEventListener('close', () => settle(reject, new Error(`${kind} socket closed before ok`)),
      { once: true });
  });
}

// Post-session feedback (thumbs + optional comment + device/browser tags). The server files it
// next to the session's recording, so pass the ConverseClient's sessionId.
export function sendFeedback({ url, sessionId, rating, text, device, browser, apiKey,
  WebSocketImpl, timeoutMs } = {}) {
  if (!url || !sessionId) return Promise.reject(new Error('url and sessionId are required'));
  const frame = { type: 'feedback', session_id: sessionId };
  if (rating) frame.rating = rating;
  if (text) frame.text = text;
  if (device) frame.device = device;
  if (browser) frame.browser = browser;
  if (apiKey) frame.api_key = apiKey;
  return sendOneShotFrame('feedback', frame, { url, WebSocketImpl, timeoutMs });
}

// Client-side failure report (mic permission denied, reconnect gave up, …): without it these are
// invisible server-side — the session log just shows a start with 0s of mic audio. The server
// journals it and, when the session's recording dir exists, files it alongside as
// client_errors.jsonl. sessionId is optional (some failures predate any session).
export function sendClientError({ url, sessionId, detail, context, apiKey,
  WebSocketImpl, timeoutMs } = {}) {
  if (!url || !detail) return Promise.reject(new Error('url and detail are required'));
  const frame = { type: 'client_error', detail };
  if (sessionId) frame.session_id = sessionId;
  if (context) frame.context = context;
  if (apiKey) frame.api_key = apiKey;
  return sendOneShotFrame('client_error', frame, { url, WebSocketImpl, timeoutMs });
}

const CONVERSE_MODE_FIELDS = new Set([
  'kind', 'voice', 'instructions', 'tools', 'web_search', 'flow', 'greeting', 'temperature',
]);
const RELAY_MODE_FIELDS = new Set(['kind', 'provider', 'model', 'voice', 'web_search']);

function validatedMode(value = { kind: 'converse' }) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError('mode must be an object');
  }
  const mode = { ...value };
  for (const [key, item] of Object.entries(mode)) {
    if (item === undefined) delete mode[key];
  }
  const allowed = mode.kind === 'converse' ? CONVERSE_MODE_FIELDS
    : mode.kind === 'relay' ? RELAY_MODE_FIELDS : null;
  if (!allowed) throw new TypeError('mode.kind must be converse or relay');
  const extra = Object.keys(mode).find((key) => !allowed.has(key));
  if (extra) throw new TypeError(`unexpected ${mode.kind} mode field: ${extra}`);
  const optionalString = (key) => {
    if (Object.hasOwn(mode, key) && typeof mode[key] !== 'string') {
      throw new TypeError(`${mode.kind} ${key} must be a string`);
    }
  };
  const optionalBoolean = (key) => {
    if (Object.hasOwn(mode, key) && typeof mode[key] !== 'boolean') {
      throw new TypeError(`${mode.kind} ${key} must be a boolean`);
    }
  };
  optionalString('voice');
  optionalBoolean('web_search');
  mode.web_search ??= false;
  if (mode.kind === 'converse') {
    optionalString('instructions');
    optionalBoolean('flow');
    if (Object.hasOwn(mode, 'tools') && !Array.isArray(mode.tools)) {
      throw new TypeError('converse tools must be an array');
    }
    if (mode.greeting != null && mode.greeting !== false && typeof mode.greeting !== 'string') {
      throw new TypeError('converse greeting must be a string or false');
    }
    if (Object.hasOwn(mode, 'temperature') && (typeof mode.temperature !== 'number'
        || !Number.isFinite(mode.temperature))) {
      throw new TypeError('converse temperature must be a finite number');
    }
  } else {
    if (typeof mode.provider !== 'string' || !mode.provider.trim()) {
      throw new TypeError('relay mode provider is required');
    }
    optionalString('model');
  }
  return mode;
}

export class ConverseClient extends EventTarget {
  constructor({ url, sessionId = createSessionId(), player, apiKey,
    mode = { kind: 'converse' }, user, timezone, rawAssist = false,
    playAcknowledgements = true, WebSocketImpl = globalThis.WebSocket,
    echoCancellerFactory = () => new EchoCanceller(),
    autoReconnect = true, reconnectBaseMs = 500, reconnectMaxMs = 5000,
    maxReconnectAttempts = 12, listeningWarmupFrames = LISTENING_WARMUP_FRAMES } = {}) {
    super();
    if (!url) throw new Error('url is required');
    if (!WebSocketImpl) throw new Error('WebSocket is required');
    this.url = toWebSocketUrl(url);
    this.sessionId = sessionId;
    this.player = player || new StreamingPlayer();
    this.apiKey = apiKey || null;
    this._mode = Object.freeze(validatedMode(mode));
    // Optional stable user identifier (e.g. a persistent anonymous id) — recorded server-side so
    // captures can be grouped per user across sessions. Never used for auth.
    this.user = user || null;
    // IANA timezone of this browser (Intl API): lets the server anchor "what time is it" and
    // search locale to the USER's clock instead of guessing. Omitted -> server treats as unknown.
    this.timezone = timezone || null;
    this.rawAssist = !!rawAssist;
    this.playAcknowledgements = !!playAcknowledgements;
    this._echoCancellerFactory = echoCancellerFactory;
    this.WebSocketImpl = WebSocketImpl;
    // Remote deploys drop sockets (wifi handoff, sleep, transient loss) far more than localhost.
    // Auto-reconnect with backoff keeps the mic alive across a blip; a reconnect is a fresh server
    // session (conversation context is server-side and resets), surfaced via events.
    this.autoReconnect = autoReconnect;
    this.reconnectBaseMs = reconnectBaseMs;
    this.reconnectMaxMs = reconnectMaxMs;
    this.maxReconnectAttempts = maxReconnectAttempts;
    this.ws = null;
    this.opened = null;
    this.audioQueue = Promise.resolve();
    this._responding = false;   // between `turn` and `done`/`interrupted`/`canceled`
    this._ackFrames = 0;        // binary frames armed by an `ack` event (playable outside a reply)
    this._ackGen = 0;           // bumped when ack credit is invalidated: guards in-flight ack enqueues
    this._playbackPauseSeq = null; // reversible hold; sequence rejects late resume events
    this._live = false;         // true only while a socket is open AND past `ready`
    this._closedByUser = false; // set by close() so a clean shutdown doesn't trigger reconnect
    this._temperature = undefined;
    this._noGreeting = false;
    this._listeningFired = false; // one `listening` event per live session, after mic warmup frames
    this._listeningFrames = 0;
    this.listeningWarmupFrames = Math.max(1, listeningWarmupFrames | 0);
    this._mic = null;          // SDK-owned capture (startMic); apps with custom capture never set it
    this._rawMic = null;       // desktop-only second capture; WebKit tees the primary raw capture
    this._aec = null;          // SDK-side AEC3, only on WebKit (see startMic)
    this._micStarting = null;  // in-flight/settled startMic() promise (idempotence + stop race)
    this._uplinkSeq = [0, 0];
    this._rawAssistActive = false;
    this._audioFrontend = null;  // actual SDK-owned mic/AEC path, persisted across reconnects
    this._audioFrontendFallback = false;
  }

  // The default mic path: owns getUserMedia under the locked AEC-only front-end spec (AEC on,
  // NS+AGC OFF — browser defaults enable both, measurably hurting ASR) and picks the AEC engine
  // per platform. Desktop Chrome/Firefox keep their native per-stream canceller; WebKit (all iOS
  // browsers + Mac Safari, where platform AEC is Apple VPIO with an ASR-harmful NS welded in)
  // captures fully raw and cancels with the SDK's AEC3-in-WASM fed the player's far-end tap.
  // Frames flow straight into pushMicFrame (which stays public for apps with custom capture).
  // Resolves {sdkAec, aecFallback} for UI messaging: sdkAec = the WASM canceller is active;
  // aecFallback = WebKit but the WASM engine failed to load, so the platform canceller (VPIO —
  // degraded but better than echo) is in effect. Idempotent while started; stopMic() re-arms.
  // sdkAec: 'auto' (default) = WASM canceller only where the platform can't deliver
  // AEC-only (WebKit/iOS, see needsSdkAec); true = force the SDK canceller everywhere
  // (desktop rollout of the ONE tuned AEC — gate on run_frontend.py before flipping
  // any default); false = force platform AEC.
  startMic({ workletUrl, sdkAec: sdkAecMode = 'auto' } = {}) {
    if (this._micStarting) return this._micStarting;
    const starting = (async () => {
      const webkit = needsSdkAec();
      let sdkAec = sdkAecMode === 'auto' ? webkit : !!sdkAecMode;
      let aecFallback = false;
      let rawAssist = this.rawAssist;
      let aec = null;
      if (sdkAec) {
        aec = this._echoCancellerFactory();
        try {
          await aec.init();
          aec.attachPlayer(this.player);
        } catch {
          aec.close();
          aec = null;
          sdkAec = false;
          aecFallback = true;
        }
      }
      // A second WebKit capture mutates device-wide processing and stripped AEC in production.
      // Raw assist there is valid only when this single raw capture feeds both WASM and raw uplink.
      if (rawAssist && webkit && !sdkAec) rawAssist = false;
      const mic = new MicCapture({
        processing: !sdkAec,   // platform AEC unless the SDK is cancelling
        workletUrl,
        onFrame: (frame, captureMs) => {
          this.pushMicFrame(aec ? aec.processCapture(frame) : frame, { captureMs });
          if (rawAssist && sdkAec) this.sendRawFrame(frame, { captureMs });
        },
      });
      let rawMic = null;
      try {
        await mic.start();
        if (rawAssist && !sdkAec) {
          rawMic = new MicCapture({
            processing: false,
            workletUrl,
            onFrame: (frame, captureMs) => this.sendRawFrame(frame, { captureMs }),
          });
          try {
            await rawMic.start();
          } catch {
            rawMic = null;
            rawAssist = false;       // primary uplink remains healthy; server uses AEC-only gate
          }
        }
      } catch (err) {
        aec?.close();
        await rawMic?.stop();
        throw err;
      }
      if (this._micStarting !== starting) {   // stopMic()/close() raced the start — don't leak the device
        aec?.close();
        await Promise.allSettled([mic.stop(), rawMic?.stop()]);
        return { sdkAec, aecFallback, rawAssist };
      }
      this._mic = mic;
      this._rawMic = rawMic;
      this._aec = aec;
      this._rawAssistActive = rawAssist;
      this._audioFrontend = sdkAec ? 'sdk-aec3' : 'platform-aec';
      this._audioFrontendFallback = aecFallback;
      this._sendRawAssistStatus(rawAssist);
      this._sendAudioFrontendStatus();
      return { sdkAec, aecFallback, rawAssist };
    })();
    this._micStarting = starting;
    starting.catch(() => { if (this._micStarting === starting) this._micStarting = null; });
    return starting;
  }

  // Safari requires audio playback to be unlocked from the user's gesture. Call this before any
  // async connect / mic-permission wait so later streamed assistant audio can actually play.
  unlockAudio() {
    return this.player?.ensureContext?.();
  }

  // Release the SDK-owned mic + AEC (no-op if startMic was never used). Safe to call repeatedly;
  // does not touch the socket, so a session can drop the mic and still receive/play audio.
  stopMic() {
    this._micStarting = null;
    const mic = this._mic;
    const rawMic = this._rawMic;
    this._mic = null;
    this._rawMic = null;
    this._rawAssistActive = false;
    this._audioFrontend = null;
    this._audioFrontendFallback = false;
    this._sendRawAssistStatus(false);
    this._sendAudioFrontendStatus();
    this._aec?.close();
    this._aec = null;
    return Promise.allSettled([mic?.stop(), rawMic?.stop()]);
  }

  /** Temporarily gate live microphone tracks without reopening the device. This is a transport
   *  safety primitive for explicit half-duplex integrations; normal full-duplex clients leave it
   *  enabled and rely on AEC. */
  setMicEnabled(enabled) {
    const active = !!enabled;
    for (const capture of [this._mic, this._rawMic]) {
      const stream = capture?.stream;
      const tracks = stream?.getAudioTracks?.() || stream?.getTracks?.() || [];
      for (const track of tracks) track.enabled = active;
    }
  }

  connect({ temperature, noGreeting = false } = {}) {
    if (this.opened) return this.opened;
    if (typeof noGreeting !== 'boolean') throw new TypeError('noGreeting must be a boolean');
    this._closedByUser = false;
    this._temperature = temperature;
    this._noGreeting = noGreeting;
    const opening = this._openOnce();
    this.opened = opening;
    // Initial connect failed (not a live drop, so no reconnect) — clear so a later connect() retries.
    // `_openOnce` never touches `this.opened` itself, so this and `_scheduleReconnect` are its sole
    // owners; that's what keeps a multi-attempt reconnect from leaving `opened` null while live.
    opening.catch(() => { if (this.opened === opening) this.opened = null; });
    return this.opened;
  }

  // One connection attempt. Resolves on `ready`; rejects if the socket fails or closes before ready.
  // If an already-live socket later drops (and the user didn't close it), kicks off auto-reconnect.
  // Owns only its own promise — never mutates `this.opened` (see connect()/_scheduleReconnect).
  _openOnce() {
    let mode = validatedMode(this._mode);
    if (mode.kind === 'converse') {
      if (this._temperature != null) mode.temperature = this._temperature;
      if (this._noGreeting) mode.greeting = false;
    }
    mode = validatedMode(mode);
    const start = {
      type: 'start',
      session_id: this.sessionId,
      audio: { sr: SAMPLE_RATE },
      mode,
    };
    if (this.apiKey) start.api_key = this.apiKey;
    const client = {};
    // Advertise reversible arbitration only when the supplied player really implements it.
    // A false capability delays genuine barges while audio continues playing.
    client.capabilities = this._supportsReversiblePlayback() ? ['playback_pause_v1'] : [];
    client.audio_frontend = this._audioFrontend || 'unknown';
    if (this.user) client.user = this.user;
    if (this.timezone) client.timezone = this.timezone;
    if (Object.keys(client).length) start.client = client;
    if (this.rawAssist) {
      start.audio.raw_assist = true;
      start.audio.uplink_format = UPLINK_FORMAT_TAGGED;
    }
    // Validate and serialize before opening a network resource. A circular/custom tool schema must
    // fail locally without leaking a connecting WebSocket.
    const startPayload = JSON.stringify(start);
    const ws = new this.WebSocketImpl(this.url);
    this.ws = ws;
    ws.binaryType = 'arraybuffer';
    return new Promise((resolve, reject) => {
      let settled = false;
      const fail = (err) => {
        if (!settled) {
          settled = true;
          reject(err instanceof Error ? err : new Error(String(err)));
        }
      };
      ws.addEventListener('open', () => { ws.send(startPayload); }, { once: true });
      ws.addEventListener('error', () => fail(new Error('Converse WebSocket failed')), { once: true });
      ws.addEventListener('close', (ev) => {
        this.ws = null;
        this._live = false;
        if (!settled) fail(new Error('Converse WebSocket closed before ready'));
        else if (!this._closedByUser && ev.code === 1000) {
          // The server hung up ON PURPOSE (only intentional ends close 1000 — e.g. the idle
          // sign-off's `close(1000, "idle")`). Redialing here would open a fresh session and
          // replay the greeting; stay closed and let the app decide. Abnormal drops (1006 loss,
          // 1011 upstream lost, 1013 drain) still reconnect below.
          this.opened = null;
          this._responding = false;   // a reused client must not carry reply/ack state into
          this._dropAck();            // a later connect() (mirrors _scheduleReconnect's resets)
          this._dispatch({ type: 'session_end', code: ev.code, reason: ev.reason || '' });
        }
        else if (!this._closedByUser && this.autoReconnect) this._scheduleReconnect();
        else this.opened = null;
      });
      ws.addEventListener('message', async (ev) => {
        try {
          const detail = await this._message(ev.data);
          if (!settled && detail?.type === 'ready') {
            settled = true;
            this._live = true;
            this._listeningFired = false;   // re-arm: this session emits `listening` after warmup
            this._listeningFrames = 0;
            this._uplinkSeq = [0, 0];
            if (this.rawAssist) this._sendRawAssistStatus(this._rawAssistActive);
            this._sendAudioFrontendStatus();
            resolve(this);
          } else if (!settled && detail?.type === 'error') {
            fail(new Error(detail.detail || 'Converse WebSocket rejected connection'));
          }
        } catch (err) {
          fail(err);
        }
      });
    });
  }

  // A live socket dropped unexpectedly. Reconnect with exponential backoff. `this.opened` tracks the
  // WHOLE reconnect chain (not each attempt) so in-flight callers (connect()/appendAudio) await the
  // live socket and never spawn a duplicate; it stays the eventually-resolved chain on success and is
  // cleared only on terminal give-up. Emits `reconnecting` then `reconnected` (or terminal `error`).
  _scheduleReconnect() {
    this._live = false;
    this._responding = false;
    this._dropAck();   // a leftover counter must not arm the fresh session's first frames
    this.player?.clear?.();
    this._dispatch({ type: 'reconnecting' });
    let attempt = 0;
    const attemptOnce = () => {
      if (this._closedByUser) return Promise.reject(new Error('closed by user'));
      attempt += 1;
      return this._openOnce().then((self) => {
        if (this._closedByUser) { try { this.ws?.close(1000); } catch { /* noop */ } return self; }
        this._dispatch({ type: 'reconnected' });
        return self;
      }).catch((err) => {
        if (this._closedByUser) throw err;
        if (attempt >= this.maxReconnectAttempts) {
          this._dispatch({ type: 'error', detail: 'reconnect failed', error: err });
          throw err;
        }
        const delay = Math.min(this.reconnectMaxMs, this.reconnectBaseMs * 2 ** (attempt - 1));
        return new Promise((res) => setTimeout(res, delay)).then(attemptOnce);
      });
    };
    const chain = attemptOnce();
    this.opened = chain;
    // On terminal give-up (or user-close), drop the rejected chain so a later connect() can retry.
    chain.then(null, () => { if (this.opened === chain) this.opened = null; });
  }

  _dispatch(event) {
    this.dispatchEvent(new CustomEvent(event.type, { detail: event }));
    this.dispatchEvent(new CustomEvent('event', { detail: event }));
  }

  _supportsReversiblePlayback() {
    return typeof this.player?.pause === 'function' && typeof this.player?.resume === 'function';
  }

  _sendAudioFrontendStatus() {
    const ws = this.ws;
    if (ws?.readyState === 1) {
      ws.send(JSON.stringify({
        type: 'client_event', event: 'audio_frontend',
        frontend: this._audioFrontend || 'unknown',
        fallback: this._audioFrontendFallback,
      }));
    }
  }

  _sendRawAssistStatus(active) {
    const ws = this.ws;
    if (ws?.readyState === 1) {
      ws.send(JSON.stringify({ type: 'raw_assist_status', active: !!active }));
    }
  }

  _uplink(frame, channel, captureMs) {
    const sequence = this._uplinkSeq[channel] >>> 0;
    this._uplinkSeq[channel] = (sequence + 1) >>> 0;
    return encodeTaggedPcm16(frame, { channel, sequence, captureMs });
  }

  async appendAudio(frame, { temperature, captureMs = captureClockMs() } = {}) {
    await this.connect({ temperature });
    if (this.ws?.readyState !== 1) return;   // dropped mid-flight — skip this realtime frame
    if (temperature != null) this.ws.send(JSON.stringify({ type: 'config', temperature }));
    this.ws.send(this.rawAssist
      ? this._uplink(frame, UPLINK_CHANNEL_PROCESSED, captureMs)
      : floatToPcm16Bytes(frame));
  }

  // Optional DEV ablation: send an UN-processed mic frame (a parallel getUserMedia track with
  // browser DSP off) on a `raw_audio` control. The server records it to raw.wav only — it never
  // drives the conversation. Fire-and-forget; silently no-ops if the socket isn't open.
  sendRawFrame(frame, { captureMs = captureClockMs() } = {}) {
    const ws = this.ws;
    if (!ws || ws.readyState !== 1) return;
    if (this.rawAssist) {
      // Custom capture integrations do not call startMic(), so the first actual raw frame is
      // their availability signal. This keeps the server fail-closed until both channels exist.
      if (!this._rawAssistActive) {
        this._rawAssistActive = true;
        this._sendRawAssistStatus(true);
      }
      ws.send(this._uplink(frame, UPLINK_CHANNEL_RAW, captureMs));
    } else {
      ws.send(JSON.stringify({ type: 'raw_audio', pcm_b64: bytesToBase64(floatToPcm16Bytes(frame)) }));
    }
  }

  // Hand the SDK each 512-sample mic frame; it streams the frame up. There is no local detection —
  // the server owns barge-in — so this just uploads.
  pushMicFrame(frame, { temperature, captureMs = captureClockMs() } = {}) {
    // Not connected (initial connect still pending, or mid-reconnect) — drop the frame rather than
    // buffer it. Buffered mic audio would flush as a stale burst into the fresh session on reconnect.
    if (!this._live) return;
    // After a short run of uploaded frames, the mic is truly capturing and Chrome's AEC has had an
    // adaptation beat. Emit `listening` then so the app does not invite speech into a cold AEC path
    // that can suppress opening syllables.
    if (!this._listeningFired) {
      this._listeningFrames += 1;
      if (this._listeningFrames >= this.listeningWarmupFrames) {
        this._listeningFired = true;
        this._dispatch({ type: 'listening' });
      }
    }
    // This path is already live, so send synchronously. Besides avoiding a needless microtask,
    // this guarantees that WebKit's processed frame is on the wire before the raw tee from the
    // same capture callback. Desktop's independent captures remain correlated by capture_ms.
    if (temperature != null) this.ws.send(JSON.stringify({ type: 'config', temperature }));
    this.ws.send(this.rawAssist
      ? this._uplink(frame, UPLINK_CHANNEL_PROCESSED, captureMs)
      : floatToPcm16Bytes(frame));
  }

  get mode() { return this._mode; }

  get responding() { return this._responding; }

  async reset() {
    this.player?.clear?.();
    this._responding = false;
    this._dropAck();
    await this.connect();
    if (this.ws?.readyState === 1) this.ws.send(JSON.stringify({ type: 'reset' }));
  }

  /** Tell the server the client's ambience layer went on/off. Recorded in the session
   *  timeline as a preference signal; has no effect on the audio pipeline. */
  sendAmbienceState(active) {
    if (this.ws?.readyState === 1) {
      this.ws.send(JSON.stringify({ type: 'ambience', active: !!active }));
    }
  }

  /** Switch character voice mid-session. Applies from the next reply; Converse mode only. */
  setVoice(voice) {
    // Relay providers bind their voice when the upstream session is constructed and do not support
    // this control. Converse reconnects replay the selected voice.
    if (this._mode.kind !== 'converse') return;
    this._mode = Object.freeze(validatedMode({ ...this._mode, voice }));
    if (this.ws?.readyState === 1) {
      this.ws.send(JSON.stringify({ type: 'set_voice', voice }));
    }
  }

  close() {
    this._closedByUser = true;   // stop any reconnect loop and prevent reconnect on the close event
    this._live = false;
    this._dropAck();             // armed-but-unsent ack frames must not bleed into a reused client

    this.stopMic();              // release the SDK-owned mic (no-op for custom-capture apps)
    this.player?.stop?.();
    try { this.ws?.close(1000); } catch { /* closing a CONNECTING socket is allowed and harmless */ }
  }

  /** Close cleanly and wait for the WebSocket close handshake, so callers can safely send
   *  follow-up controls that depend on server-side session finalization. */
  async closeAndWait(timeoutMs = 5000) {
    const ws = this.ws;
    if (!ws || ws.readyState === 3) {
      this.close();
      return;
    }
    const closed = new Promise((resolve) => {
      const timer = setTimeout(resolve, timeoutMs);
      ws.addEventListener('close', () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
    });
    this.close();
    await closed;
  }

  // Disarm ack credit and invalidate any in-flight ack enqueue (the gen recheck in _message).
  // The server only schedules an ack when no reply is active, so ack frames and reply events
  // never legitimately interleave — if a `turn`/`canceled`/`interrupted` arrives while credit
  // is armed, the frames that follow are the reply's, and counting them as ack would route
  // them around the cancellation recheck (stray audio after a barge/rescind).
  _dropAck() {
    this._ackFrames = 0;
    this._ackGen++;
  }

  // Drive playback state off each server event, before re-dispatching it.
  _applyReflex(event) {
    switch (event.type) {
      case 'turn':
        this._responding = true;
        this._playbackPauseSeq = null;
        this._dropAck();
        break;
      case 'playback_pause': {
        const seq = Number.isInteger(event.pause_seq) ? event.pause_seq : 0;
        this._playbackPauseSeq = seq;
        this.audioQueue = this.audioQueue.catch(() => {}).then(async () => {
          if (!this._responding || this._playbackPauseSeq !== seq) return;
          try {
            if (!this._supportsReversiblePlayback()) {
              throw new Error('player does not implement reversible pause and resume');
            }
            await this.player.pause();
          } catch (error) {
            if (this.ws?.readyState === 1 && this._playbackPauseSeq === seq) {
              this.ws.send(JSON.stringify({
                type: 'client_event', event: 'playback_pause_failed', pause_seq: seq,
                detail: String(error?.message || error).slice(0, 160),
              }));
            }
            return;
          }
          if (this.ws?.readyState === 1 && this._playbackPauseSeq === seq) {
            this.ws.send(JSON.stringify({
              type: 'client_event', event: 'playback_paused', pause_seq: seq,
              pending_ms: Math.round(this.player?.pendingMs?.() ?? 0),
            }));
          }
        });
        break;
      }
      case 'playback_resume': {
        const seq = Number.isInteger(event.pause_seq) ? event.pause_seq : 0;
        if (this._playbackPauseSeq !== seq) break;
        this._playbackPauseSeq = null;
        this.audioQueue = this.audioQueue.catch(() => {}).then(async () => {
          await this.player?.resume?.();
          if (this.ws?.readyState === 1) {
            this.ws.send(JSON.stringify({
              type: 'client_event', event: 'playback_resumed', pause_seq: seq,
            }));
          }
        });
        break;
      }
      case 'canceled':    // eager speculation retracted — the audio was a mistake, clear it
        this._playbackPauseSeq = null;
        this.player?.clear?.();
        this._responding = false;
        this._dropAck();
        break;
      case 'interrupted': // barged — stop the reply (fade-clear if the server asks, else drain).
        this._playbackPauseSeq = null;
        this._responding = false;
        this._dropAck();
        // Report the stop so the server can timestamp actual silence at the speaker
        // (barge_detected -> playback_stopped = the stop half of barge latency) and, on a
        // clear, re-truncate its committed text by discarded_ms (audio the user never heard);
        // barge_seq is echoed so a slow report can't be applied to a later barge. Chained on
        // audioQueue so in-flight enqueues (dropped by the _responding recheck) settle first.
        this.audioQueue = this.audioQueue.catch(() => {}).then(() => {
          const pending = this.player?.pendingMs?.() ?? 0;
          const device = this.player?.deviceLatencyMs?.() ?? 0;
          let remaining = pending + device;
          let discarded = 0;
          if (event.clear) {
            const wasPaused = !!this.player?.paused;
            this.player?.clear?.(BARGE_CLEAR_FADE_S);
            const fadeMs = wasPaused ? 0 : BARGE_CLEAR_FADE_S * 1000;
            discarded = Math.max(0, pending - fadeMs);
            remaining = Math.min(pending, fadeMs) + device;
          }
          if (this.ws?.readyState === 1) {
            const report = { type: 'client_event', event: 'playback_stopped',
                             remaining_ms: Math.round(remaining),
                             discarded_ms: Math.round(discarded) };
            if (typeof event.barge_seq === 'number') report.barge_seq = event.barge_seq;
            this.ws.send(JSON.stringify(report));
          }
        });
        break;
      case 'done':
        this._responding = false;
        if (this._playbackPauseSeq !== null) {
          this._playbackPauseSeq = null;
          this.audioQueue = this.audioQueue.catch(() => {}).then(() => this.player?.resume?.());
        }
        break;
      case 'ack':
        // Assistant backchannel clip ("mm-hmm") sent OUTSIDE a reply: the next
        // `frames` binary messages are playable even though _responding is false.
        this._ackFrames = this.playAcknowledgements ? (event.frames | 0) : 0;
        break;
      default:
        break;
    }
  }

  async _message(data) {
    if (typeof data === 'string') {
      const event = JSON.parse(data);
      this._applyReflex(event);
      this.dispatchEvent(new CustomEvent(event.type, { detail: event }));
      this.dispatchEvent(new CustomEvent('event', { detail: event }));
      return event;
    }
    const samples = await binaryToFloat32(data);
    const detail = { type: 'audio', sr: SAMPLE_RATE, samples };
    this.dispatchEvent(new CustomEvent('audio', { detail }));
    this.dispatchEvent(new CustomEvent('event', { detail }));
    if (this._ackFrames > 0) {
      // ack clip audio: short, deliberately not barge-cleared — but the gen recheck below keeps
      // an in-flight enqueue from playing after _dropAck invalidated it (mirrors the
      // _responding recheck in the reply branch).
      this._ackFrames--;
      const gen = this._ackGen;
      this.audioQueue = this.audioQueue
        .catch(() => {})
        .then(() => { if (gen === this._ackGen) return this.player?.enqueue?.(samples); })
        .catch((err) => {
          this.dispatchEvent(new CustomEvent('error', { detail: { type: 'error', error: err } }));
        });
      await this.audioQueue;
    } else if (this._responding) {
      this.audioQueue = this.audioQueue
        .catch(() => {})
        // Re-check responding AFTER the await: a `canceled` can land (and clear the player) while
        // this enqueue is in flight; without the recheck the discarded tail would resurrect playback.
        .then(() => { if (this._responding) return this.player?.enqueue?.(samples); })
        .catch((err) => {
          this.dispatchEvent(new CustomEvent('error', { detail: { type: 'error', error: err } }));
        });
      await this.audioQueue;
    }
    return detail;
  }
}
