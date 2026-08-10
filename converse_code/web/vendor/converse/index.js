import {
  binaryToFloat32, bytesToBase64, createSessionId, encodeTaggedPcm16,
  floatToPcm16Bytes, SAMPLE_RATE, toWebSocketUrl,
  UPLINK_CHANNEL_PROCESSED, UPLINK_CHANNEL_RAW, UPLINK_FORMAT_TAGGED,
} from './audio.js';
import { StreamingPlayer } from './player.js';
import { EchoCanceller, needsSdkAec } from './aec.js';
import { MicCapture } from './mic.js';
import { TrackFeeder, WebRtcSession } from './webrtc.js';

export {
  FRAME_SAMPLES, SAMPLE_RATE, binaryToFloat32, createSessionId, encodeTaggedPcm16,
  floatToPcm16Bytes, toWebSocketUrl,
  UPLINK_CHANNEL_PROCESSED, UPLINK_CHANNEL_RAW, UPLINK_FORMAT_TAGGED,
} from './audio.js';
export { StreamingPlayer } from './player.js';
export { EchoCanceller, needsSdkAec } from './aec.js';
export { MicCapture } from './mic.js';
export { TrackFeeder, WebRtcSession } from './webrtc.js';

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
function brokerError(frame, fallback) {
  const err = new Error(frame?.detail || fallback);
  err.code = frame?.code;
  err.retryable = frame?.retryable;
  return err;
}

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
  'silence_nudge_s', 'silence_end_s',
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
    // Per-session override of the broker's two-stage silence policy (env defaults: 10s/20s) — e.g.
    // a benchmark harness with long simulated-user think-time. Omit either field to keep the
    // broker's default for it; the broker also falls back to its defaults if these are omitted,
    // non-positive, or silence_end_s <= silence_nudge_s.
    for (const key of ['silence_nudge_s', 'silence_end_s']) {
      if (Object.hasOwn(mode, key) && (typeof mode[key] !== 'number'
          || !Number.isFinite(mode[key]) || mode[key] <= 0)) {
        throw new TypeError(`converse ${key} must be a positive finite number`);
      }
    }
    if (Object.hasOwn(mode, 'silence_nudge_s') && Object.hasOwn(mode, 'silence_end_s')
        && mode.silence_end_s <= mode.silence_nudge_s) {
      throw new TypeError('converse silence_end_s must be greater than silence_nudge_s');
    }
  } else {
    if (typeof mode.provider !== 'string' || !mode.provider.trim()) {
      throw new TypeError('relay mode provider is required');
    }
    optionalString('model');
  }
  return mode;
}

function resumeTokenFromState(state) {
  if (state == null) return null;
  if (typeof state !== 'object' || Array.isArray(state)) {
    throw new TypeError('resumeState must be an object or null');
  }
  if (state.version !== 1) {
    throw new TypeError('resumeState.version must be 1');
  }
  if (typeof state.resumeToken !== 'string' || !state.resumeToken) {
    throw new TypeError('resumeState.resumeToken must be a non-empty string');
  }
  return state.resumeToken;
}

export class ConverseClient extends EventTarget {
  constructor({ url, sessionId = createSessionId(), player, apiKey,
    mode = { kind: 'converse' }, user, timezone, rawAssist = false,
    playAcknowledgements = true, WebSocketImpl = globalThis.WebSocket,
    echoCancellerFactory = () => new EchoCanceller(),
    autoReconnect = true, reconnectBaseMs = 500, reconnectMaxMs = 5000,
    maxReconnectAttempts = 12, listeningWarmupFrames = LISTENING_WARMUP_FRAMES,
    resumeState = null,
    transport = 'ws', RTCPeerConnectionImpl = globalThis.RTCPeerConnection } = {}) {
    super();
    if (!url) throw new Error('url is required');
    if (!WebSocketImpl) throw new Error('WebSocket is required');
    if (transport !== 'ws' && transport !== 'webrtc') {
      throw new TypeError('transport must be "ws" or "webrtc"');
    }
    if (transport === 'webrtc' && needsSdkAec()) {
      // WebKit's echo cancellation is the SDK's own AEC3 canceller, and its far-end reference
      // comes from the WS player's scheduled chunks — assistant audio over webrtc bypasses that
      // player, which would leave Safari/iOS with NO echo cancellation at all (barge-in and ASR
      // both break against speaker bleed). Fall back to the WS transport until the webrtc path
      // grows a remote-track far-end tap (tracked TODO).
      console.warn('[converse] webrtc transport is not yet supported on WebKit — using ws');
      transport = 'ws';
    }
    this.url = toWebSocketUrl(url);
    this.transport = transport;
    this._RTCPeerConnectionImpl = RTCPeerConnectionImpl;
    this.sessionId = sessionId;
    this.player = player || new StreamingPlayer();
    this.apiKey = apiKey || null;
    // Dual real-time capture channels (tagged binary uplink) ride WS binary frames; webrtc has a
    // single outbound audio track, so raw_assist ablation isn't representable there yet (TODO).
    if (transport === 'webrtc' && rawAssist) rawAssist = false;
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
    // Auto-reconnect with backoff keeps the mic alive across a blip; the server-issued resume token
    // preserves conversation and deferred jobs during its bounded reconnect window.
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
    this._resumeToken = resumeTokenFromState(resumeState);
    // Latest server token (possibly imported above); sent on the next continuation attempt.
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
    // webrtc-transport-only state (see _openOnceWebRtc): the peer connection, its "control" data
    // channel (the send-a-control-frame primitive's destination instead of `this.ws`), the mic's
    // re-injection feeder (src/webrtc.js), and the hidden <audio> element assistant audio plays
    // through. None of these are touched when transport === 'ws'.
    this._rtcSession = null;
    this._channel = null;
    this._trackFeeder = null;
    this._micSender = null;           // the pc's outbound-audio RTCRtpSender (see startMic below)
    this._directMicTrackEngaged = false;  // true once startMic() swapped the raw device track in
    this._remoteAudioEl = null;
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
      // The normal webrtc path: swap the getUserMedia track straight into the RTCPeerConnection's
      // sender instead of leaving the JS TrackFeeder re-injection in the loop. webrtc only ever
      // runs on non-WebKit platforms (WebKit forces ws — see needsSdkAec() above this class), so
      // `!sdkAec` here means native platform echo cancellation already produced this exact track's
      // audio — it's the SAME processed audio the feeder would otherwise have re-encoded, with zero
      // extra JS hops/queueing/latency. replaceTrack() needs no renegotiation (the sender/m-line
      // were already established by the initial offer in _openOnceWebRtc). Only the SDK's own AEC3
      // canceller (sdkAec, WebKit-only in practice) or a custom-capture caller (no startMic() at
      // all) still needs the feeder — see _uplinkFrame.
      if (this.transport === 'webrtc' && !sdkAec && this._micSender) {
        const rawTrack = mic.stream?.getAudioTracks?.()[0];
        if (rawTrack) {
          this._micSender.replaceTrack(rawTrack)
            .then(() => { this._directMicTrackEngaged = true; })
            .catch((err) => console.warn(
              '[voice-loop] webrtc direct mic track swap failed; staying on the feeder', err));
        }
      }
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
  // Over webrtc the StreamingPlayer is unused (see _attachRemoteAudio) — the hidden <audio> element
  // is the only playback surface, so do not allocate a silent AudioContext for that transport.
  // Nudge an existing element too (for example after a reconnect established a new remote track).
  unlockAudio() {
    const contextUnlock = this.transport === 'ws' ? this.player?.ensureContext?.() : null;
    return Promise.all([
      contextUnlock,
      this._remoteAudioEl?.play?.().catch(() => {}),
    ].filter(Boolean));
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
    // Hand the sender back to the feeder BEFORE the capture's own track is stopped below, so the
    // peer connection never ends up pointing at an ended track (and any later pushMicFrame() from
    // a custom-capture caller has somewhere to go again).
    let handoff = Promise.resolve();
    if (this._directMicTrackEngaged && this._micSender) {
      this._directMicTrackEngaged = false;
      handoff = this._micSender.replaceTrack(this._trackFeeder?.track || null).catch(() => {});
    }
    return handoff.then(() => Promise.allSettled([mic?.stop(), rawMic?.stop()]));
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
    // webrtc: the outbound track is the feeder's synthetic MediaStreamTrack, not the capture's own
    // getUserMedia track (see src/webrtc.js) — mute it directly so custom-capture apps (which call
    // pushMicFrame() without startMic() and so have no capture track above) still get silence.
    this._trackFeeder?.setMuted(!active);
  }

  connect({ temperature, noGreeting = false } = {}) {
    if (this.opened) return this.opened;
    if (typeof noGreeting !== 'boolean') throw new TypeError('noGreeting must be a boolean');
    this._closedByUser = false;
    this._temperature = temperature;
    this._noGreeting = noGreeting;
    const opening = this.transport === 'webrtc' ? this._openOnceWebRtc() : this._openOnce();
    this.opened = opening;
    // Initial connect failed (not a live drop, so no reconnect) — clear so a later connect() retries.
    // `_openOnce` never touches `this.opened` itself, so this and `_scheduleReconnect` are its sole
    // owners; that's what keeps a multi-attempt reconnect from leaving `opened` null while live.
    opening.catch((err) => {
      if (this.opened === opening) this.opened = null;
      if (err?.code === 'resume_failed' && this._resumeToken) {
        this._setResumeToken(null);
        this._dispatch({ type: 'resume_failed', error: err });
      }
    });
    return this.opened;
  }

  /** Return the current opaque, JSON-serializable continuation state, or null before `ready` and
   *  after the session ends. Persist it only in storage appropriate for a short-lived credential
   *  (normally sessionStorage), then pass it back as `resumeState` after a page reload. */
  exportResumeState() {
    return this._resumeToken ? { version: 1, resumeToken: this._resumeToken } : null;
  }

  /** Install state previously returned by exportResumeState(). Import is deliberately restricted
   *  to a client that has not started connecting: replacing a live session's continuation token
   *  would make the next automatic reconnect resume unrelated context. */
  importResumeState(state) {
    if (this.opened || this._live || this.ws) {
      throw new Error('resume state can only be imported before connect()');
    }
    this._setResumeToken(resumeTokenFromState(state));
  }

  _setResumeToken(token) {
    const normalized = typeof token === 'string' && token ? token : null;
    if (normalized === this._resumeToken) return;
    this._resumeToken = normalized;
    this._dispatch({ type: 'resume_state', state: this.exportResumeState() });
  }

  // Shared start-frame construction (mode/temperature/greeting/capabilities/rawAssist) for both
  // transports. webrtc mode-kind capability advertising: reversible playback_pause/resume needs a
  // player the client can actually pause on command, which the remote-track path doesn't implement
  // yet (see _applyReflex's transport guards) — so webrtc never advertises playback_pause_v1
  // regardless of the configured player (TODO once/if reversible pause targets the <audio> element).
  _buildStartFrame() {
    let mode = validatedMode(this._mode);
    if (mode.kind === 'converse') {
      if (this._temperature != null) mode.temperature = this._temperature;
      if (this._noGreeting) mode.greeting = false;
    }
    mode = validatedMode(mode);
    const start = {
      type: 'start',
      session_id: this.sessionId,
      audio: { sr: SAMPLE_RATE, output_encoding: 'pcm16' },
      mode,
    };
    if (this.apiKey) start.api_key = this.apiKey;
    if (this._resumeToken) start.resume_token = this._resumeToken;
    const client = {};
    client.capabilities = (this.transport !== 'webrtc' && this._supportsReversiblePlayback())
      ? ['playback_pause_v1'] : [];
    client.audio_frontend = this._audioFrontend || 'unknown';
    if (this.user) client.user = this.user;
    if (this.timezone) client.timezone = this.timezone;
    if (Object.keys(client).length) start.client = client;
    if (this.rawAssist) {
      start.audio.raw_assist = true;
      start.audio.uplink_format = UPLINK_FORMAT_TAGGED;
    }
    return start;
  }

  // One connection attempt. Resolves on `ready`; rejects if the socket fails or closes before ready.
  // If an already-live socket later drops (and the user didn't close it), kicks off auto-reconnect.
  // Owns only its own promise — never mutates `this.opened` (see connect()/_scheduleReconnect).
  _openOnce() {
    const start = this._buildStartFrame();
    // Validate and serialize before opening a network resource. A circular/custom tool schema must
    // fail locally without leaking a connecting WebSocket.
    const startPayload = JSON.stringify(start);
    const ws = new this.WebSocketImpl(this.url);
    this.ws = ws;
    ws.binaryType = 'arraybuffer';
    return new Promise((resolve, reject) => {
      let settled = false;
      let liveReady = false;
      const fail = (err) => {
        if (!settled) {
          settled = true;
          // Don't leak a live socket when connect() rejects (error frame, format mismatch):
          // the server would keep the session open while the app believes connect failed.
          if (ws.readyState === 0 || ws.readyState === 1) {
            try { ws.close(1000, 'client rejected session'); } catch { /* already closing */ }
          }
          reject(err instanceof Error ? err : new Error(String(err)));
        }
      };
      ws.addEventListener('open', () => { ws.send(startPayload); }, { once: true });
      ws.addEventListener('error', () => fail(new Error('Converse WebSocket failed')), { once: true });
      ws.addEventListener('close', (ev) => {
        this.ws = null;
        this._live = false;
        if (!liveReady) {
          if (!settled) fail(new Error('Converse WebSocket closed before ready'));
          return;
        }
        if (!this._closedByUser && ev.code === 1000) {
          // The server hung up ON PURPOSE (only intentional ends close 1000 — e.g. the idle
          // sign-off's `close(1000, "idle")`). Redialing here would open a fresh session and
          // replay the greeting; stay closed and let the app decide. Abnormal drops (1006 loss,
          // 1011 upstream lost, 1013 drain) still reconnect below.
          this.opened = null;
          this._setResumeToken(null); // an ended session must not resume on a later connect()
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
            // The ready frame states the negotiated downlink format; a mismatch here would
            // otherwise surface as noise in the speakers, so fail the connect loudly instead.
            const fmt = detail.audio;
            if (fmt && (fmt.output_encoding !== 'pcm16' || fmt.output_sr !== SAMPLE_RATE)) {
              fail(new Error(`server negotiated unsupported downlink audio ${JSON.stringify(fmt)}; `
                + `this SDK plays pcm16 at ${SAMPLE_RATE} Hz`));
              return;
            }
            if (typeof detail.resume_token === 'string') this._setResumeToken(detail.resume_token);
            settled = true;
            liveReady = true;
            this._live = true;
            this._listeningFired = false;   // re-arm: this session emits `listening` after warmup
            this._listeningFrames = 0;
            this._uplinkSeq = [0, 0];
            if (this.rawAssist) this._sendRawAssistStatus(this._rawAssistActive);
            this._sendAudioFrontendStatus();
            resolve(this);
          } else if (!settled && detail?.type === 'error') {
            fail(brokerError(detail, 'Converse WebSocket rejected connection'));
          }
        } catch (err) {
          fail(err);
        }
      });
    });
  }

  // The webrtc counterpart of _openOnce(): signaling still rides a plain WebSocket to the same
  // /ws URL (see serving/broker_webrtc.py), but only long enough to exchange the SDP offer/answer —
  // once the "control" data channel is open, every protocol frame this class sends/receives moves
  // through _sendControl()/_message() over the channel instead, unchanged.
  //
  // Reconnect TODO: unlike _openOnce, this never calls _scheduleReconnect() — autoReconnect stays a
  // WS-transport-only feature for this first implementation (no ICE-restart support yet). A
  // dropped/failed peer connection surfaces exactly the events a dead WS with autoReconnect:false
  // would (see the channel/connectionstatechange handlers below), so callers see a consistent
  // "terminal" shape either way; a future revision can add ICE-restart-based reconnect here without
  // changing that external contract.
  async _openOnceWebRtc() {
    const start = this._buildStartFrame();
    start.transport = { kind: 'webrtc' };   // no sdp yet — TURN creds must exist before we gather
    this._teardownWebRtc();   // a stale peer connection/feeder from a previous failed attempt

    const startPayload = JSON.stringify(start);
    const ws = new this.WebSocketImpl(this.url);   // signaling only — closed once the answer lands
    this.ws = ws;

    return new Promise((resolve, reject) => {
      let settled = false;
      let session = null;
      let feeder = null;
      let channel = null;
      // The signaling socket is deliberately self-closed right after the answer is applied (its
      // job is done — wire contract note #5); that close must NOT be mistaken for the signaling
      // socket dying before an answer ever arrived.
      let signalingDone = false;
      const fail = (err) => {
        if (!settled) {
          settled = true;
          this._teardownWebRtc();
          try { ws.close(); } catch { /* already closing/closed */ }
          reject(err instanceof Error ? err : new Error(String(err)));
        }
      };

      ws.addEventListener('open', () => { ws.send(startPayload); }, { once: true });
      ws.addEventListener('error', () => fail(new Error('Converse signaling WebSocket failed')), { once: true });
      ws.addEventListener('close', () => {
        if (this.ws === ws) this.ws = null;
        if (!settled && !signalingDone) fail(new Error('signaling socket closed before webrtc_answer'));
        // Once settled (or once we closed it ourselves post-answer) the signaling socket has done
        // its job — a close here, expected or not, has no bearing on the live call.
      });
      ws.addEventListener('message', async (ev) => {
        if (typeof ev.data !== 'string') return;   // signaling only ever carries JSON
        let msg;
        try { msg = JSON.parse(ev.data); } catch (err) { fail(err); return; }
        if (msg.type === 'webrtc_ice') {
          // Step 1 of signaling: build the peer connection WITH the server's (possibly
          // TURN-bearing) ice_servers, gather, then offer — see the module-level wire contract
          // note. Building the RTCPeerConnection any earlier would gather before TURN creds
          // exist, making TURN permanently unusable.
          if (session) return;   // duplicate webrtc_ice — ignore, first one wins
          session = new WebRtcSession({
            RTCPeerConnectionImpl: this._RTCPeerConnectionImpl,
            iceServers: Array.isArray(msg.ice_servers) && msg.ice_servers.length
              ? msg.ice_servers : undefined,
          });
          this._rtcSession = session;
          session.onRemoteTrack((stream) => this._attachRemoteAudio(stream));
          session.onConnectionStateChange((state) => {
            if (state === 'failed' || state === 'closed') fail(new Error(`webrtc connection ${state}`));
          });
          feeder = new TrackFeeder();
          this._trackFeeder = feeder;
          try {
            await feeder.start();
            channel = session.createControlChannel();
            // This feeder track is what negotiates the offer's audio m-line (startMic() hasn't
            // necessarily run yet — connect() resolves before the app calls it). Keep the sender
            // so a later startMic() can replaceTrack() the real getUserMedia track in directly,
            // with no renegotiation needed (see startMic()).
            this._micSender = session.addAudioTrack(feeder.track);
            const sdp = await session.createOfferWithGatheredIce();
            ws.send(JSON.stringify({ type: 'webrtc_offer', sdp }));
          } catch (err) { fail(err); return; }
          channel.addEventListener('message', async (chEv) => {
            let detail;
            try { detail = await this._message(chEv.data); } catch (err) { fail(err); return; }
            if (detail?.type === 'bye') {
              // Server-initiated close over the channel — treat exactly like a WS close with
              // that code (wire contract note #5a).
              if (!settled) fail(new Error(detail.reason || 'webrtc session closed'));
              else this._handleTransportClose(detail.code, detail.reason);
              return;
            }
            if (!settled && detail?.type === 'ready') {
              if (typeof detail.resume_token === 'string') this._setResumeToken(detail.resume_token);
              settled = true;
              this._channel = channel;
              this._live = true;
              this._listeningFired = false;
              this._listeningFrames = 0;
              this._uplinkSeq = [0, 0];
              if (this.rawAssist) this._sendRawAssistStatus(this._rawAssistActive);
              this._sendAudioFrontendStatus();
              resolve(this);
            } else if (!settled && detail?.type === 'error') {
              fail(brokerError(detail, 'Converse webrtc session rejected'));
            }
          });
          channel.addEventListener('close', () => {
            if (!settled) { fail(new Error('control channel closed before ready')); return; }
            if (!this._closedByUser) this._handleTransportClose(1006, '');  // abnormal drop, no reconnect (see TODO above)
          });
        } else if (msg.type === 'webrtc_answer') {
          if (!session) { fail(new Error('webrtc_answer before webrtc_ice')); return; }
          try {
            await session.applyAnswer(msg.sdp);
          } catch (err) { fail(err); return; }
          signalingDone = true;
          try { ws.close(1000); } catch { /* noop */ }   // signaling's job is done
        } else if (msg.type === 'error') {
          fail(brokerError(msg, 'Converse webrtc connect rejected'));
        }
      });
    });
  }

  // Mirrors the WS 'close' handler's non-reconnect branches (autoReconnect:false shape) for a
  // channel/PC teardown that happens after `ready` — see _openOnceWebRtc's reconnect TODO.
  _handleTransportClose(code, reason) {
    if (!this._live) return;   // already handled (e.g. 'bye' then the channel's own 'close' event)
    this._live = false;
    this._channel = null;
    this.opened = null;
    if (code === 1000) {
      // Only an intentional server end closes 1000 (mirrors the WS idle sign-off) — surface it the
      // same way so app code doesn't need transport-specific handling.
      this._responding = false;
      this._setResumeToken(null);
      this._dropAck();
      this._dispatch({ type: 'session_end', code, reason: reason || '' });
    }
    this._teardownWebRtc();
  }

  // Assistant audio over webrtc arrives as a remote Opus track, not binary WS frames — StreamingPlayer
  // is unused here. Attach it to a hidden <audio> element instead; unlockAudio() also nudges this
  // element's play() for browsers that gate autoplay on a user gesture.
  _attachRemoteAudio(stream) {
    if (typeof document === 'undefined') return;   // non-browser test environment
    if (!this._remoteAudioEl) {
      const el = document.createElement('audio');
      el.autoplay = true;
      el.playsInline = true;
      el.style.display = 'none';
      (document.body || document.documentElement)?.appendChild(el);
      this._remoteAudioEl = el;
    }
    this._remoteAudioEl.srcObject = stream;
    this._remoteAudioEl.play?.().catch(() => {});   // best-effort; unlockAudio() retries post-gesture
  }

  _teardownWebRtc() {
    this._channel = null;
    const session = this._rtcSession;
    const feeder = this._trackFeeder;
    this._rtcSession = null;
    this._trackFeeder = null;
    this._micSender = null;
    this._directMicTrackEngaged = false;
    session?.close();
    feeder?.stop().catch(() => {});
    if (this._remoteAudioEl) {
      try { this._remoteAudioEl.pause?.(); } catch { /* noop */ }
      this._remoteAudioEl.srcObject = null;
      this._remoteAudioEl.remove?.();
      this._remoteAudioEl = null;
    }
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
        if (err?.code === 'resume_failed') {
          this._setResumeToken(null);
          this._dispatch({ type: 'resume_failed', error: err });
          throw err;
        }
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

  // The one primitive every caller uses to send protocol JSON, so index.js has exactly one place
  // that knows the wire differs by transport: over 'ws' it's ws.send(); over 'webrtc' every frame
  // that would have gone over the socket instead rides the "control" RTCDataChannel byte-for-byte
  // (see serving/broker_webrtc.py's module docstring). Silently no-ops if neither is open —
  // callers already treat that as "dropped mid-flight, best-effort".
  _sendControl(obj) {
    if (this.transport === 'webrtc') {
      if (this._channel?.readyState === 'open') this._channel.send(JSON.stringify(obj));
    } else if (this.ws?.readyState === 1) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  _sendAudioFrontendStatus() {
    this._sendControl({
      type: 'client_event', event: 'audio_frontend',
      frontend: this._audioFrontend || 'unknown',
      fallback: this._audioFrontendFallback,
    });
  }

  _sendRawAssistStatus(active) {
    this._sendControl({ type: 'raw_assist_status', active: !!active });
  }

  _uplink(frame, channel, captureMs) {
    const sequence = this._uplinkSeq[channel] >>> 0;
    this._uplinkSeq[channel] = (sequence + 1) >>> 0;
    return encodeTaggedPcm16(frame, { channel, sequence, captureMs });
  }

  // Push one mic frame onto the wire: WS binary frame, or (webrtc) into the TrackFeeder that backs
  // the outbound RTCPeerConnection audio track. rawAssist's dual tagged channel is WS-only (see the
  // constructor), so webrtc always takes the plain branch here.
  // Once startMic() has swapped the raw getUserMedia track directly into the peer connection (see
  // startMic()), the outbound audio no longer depends on this re-injection at all — pushing these
  // frames into the feeder too would just be wasted work, since its output track is no longer the
  // one actually wired to the sender. Only a custom-capture caller (pushMicFrame()/appendAudio()
  // without startMic()) or the SDK's own AEC3 canceller path still needs the feeder engaged.
  _uplinkFrame(frame, captureMs) {
    if (this.transport === 'webrtc') {
      if (!this._directMicTrackEngaged) this._trackFeeder?.push(frame);
      return;
    }
    this.ws.send(this.rawAssist
      ? this._uplink(frame, UPLINK_CHANNEL_PROCESSED, captureMs)
      : floatToPcm16Bytes(frame));
  }

  async appendAudio(frame, { temperature, captureMs = captureClockMs() } = {}) {
    await this.connect({ temperature });
    if (!this._live) return;   // dropped mid-flight — skip this realtime frame
    if (temperature != null) this._sendControl({ type: 'config', temperature });
    this._uplinkFrame(frame, captureMs);
  }

  // Optional DEV ablation: send an UN-processed mic frame (a parallel getUserMedia track with
  // browser DSP off) on a `raw_audio` control. The server records it to raw.wav only — it never
  // drives the conversation. Fire-and-forget; silently no-ops if the socket isn't open.
  // rawAssist is forced off over webrtc (constructor), so only the plain `raw_audio` control frame
  // path applies there — it still works since it's a data-channel JSON frame like any other.
  sendRawFrame(frame, { captureMs = captureClockMs() } = {}) {
    if (this.rawAssist) {
      if (this.transport === 'webrtc' || !this.ws || this.ws.readyState !== 1) return;
      // Custom capture integrations do not call startMic(), so the first actual raw frame is
      // their availability signal. This keeps the server fail-closed until both channels exist.
      if (!this._rawAssistActive) {
        this._rawAssistActive = true;
        this._sendRawAssistStatus(true);
      }
      this.ws.send(this._uplink(frame, UPLINK_CHANNEL_RAW, captureMs));
    } else {
      this._sendControl({ type: 'raw_audio', pcm_b64: bytesToBase64(floatToPcm16Bytes(frame)) });
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
    if (temperature != null) this._sendControl({ type: 'config', temperature });
    this._uplinkFrame(frame, captureMs);
  }

  get mode() { return this._mode; }

  get responding() { return this._responding; }

  async reset() {
    this.player?.clear?.();
    this._responding = false;
    this._dropAck();
    await this.connect();
    this._sendControl({ type: 'reset' });
  }

  /** Tell the server the client's ambience layer went on/off. Recorded in the session
   *  timeline as a preference signal; has no effect on the audio pipeline. */
  sendAmbienceState(active) {
    this._sendControl({ type: 'ambience', active: !!active });
  }

  /** Add a typed user message or silent host context to the conversation, optionally asking the
   *  model to reply immediately. Mirrors the public inject_context wire contract. */
  injectContext(text, { role = 'context', reply = false } = {}) {
    if (typeof text !== 'string') throw new TypeError('text must be a string');
    if (!text.trim() || [...text].length > 2000) {
      throw new RangeError('text must contain 1 to 2000 characters');
    }
    if (role !== 'user' && role !== 'context') {
      throw new TypeError('role must be "user" or "context"');
    }
    if (typeof reply !== 'boolean') throw new TypeError('reply must be a boolean');
    this._sendControl({ type: 'inject_context', text, role, reply });
  }

  /** Resolve a `tool_call` event with JSON content. Keep results compact: the server enforces
   *  its configured UTF-8 JSON byte ceiling and replaces oversized content with a bounded
   *  truncation marker and preview. Listen for calls via `client.addEventListener('tool_call', …)`;
   *  the content itself may be produced anywhere (e.g. relayed from your backend). */
  sendToolResult(id, content) {
    this._sendControl({ type: 'tool_result', id, content });
  }

  /** Detach an eligible tool call from the current voice turn. The host keeps running the job and
   *  may address later progress, cancellation, and the one terminal result by id or handle. */
  sendToolDeferred(id, { handle, statusLabel } = {}) {
    const frame = { type: 'tool_deferred', id, handle };
    if (statusLabel) frame.status_label = statusLabel;
    this._sendControl(frame);
  }

  /** Report human-readable progress on an in-flight tool call (docs/client-tool-protocol.md §3):
   *  appends to the brain's context so the next turn can speak to it; never resolves the call. */
  sendToolProgress(id, note) {
    this._sendControl({ type: 'tool_progress', id, note });
  }

  /** Deliver a structured segment of an in-flight call's eventual answer
   *  (docs/client-tool-protocol.md §3a): capped like a result envelope, and `reply: true` asks
   *  the broker to proactively narrate it now. Never resolves the call — the terminal
   *  sendToolResult is still required exactly once. */
  sendToolPartialResult(id, content, { reply = false } = {}) {
    this._sendControl({ type: 'tool_partial_result', id, content, ...(reply ? { reply: true } : {}) });
  }

  /** Cancel an in-flight tool call. */
  sendToolCancel(id) {
    this._sendControl({ type: 'tool_cancel', id });
  }

  /** Switch character voice mid-session. Applies from the next reply; Converse mode only. */
  setVoice(voice) {
    // Relay providers bind their voice when the upstream session is constructed and do not support
    // this control. Converse reconnects replay the selected voice.
    if (this._mode.kind !== 'converse') return;
    this._mode = Object.freeze(validatedMode({ ...this._mode, voice }));
    this._sendControl({ type: 'set_voice', voice });
  }

  close() {
    this._closedByUser = true;   // stop any reconnect loop and prevent reconnect on the close event
    this._live = false;
    this._setResumeToken(null); // an intentional reuse starts a new conversation
    this._dropAck();             // armed-but-unsent ack frames must not bleed into a reused client

    this.stopMic();              // release the SDK-owned mic (no-op for custom-capture apps)
    this.player?.stop?.();
    if (this.transport === 'webrtc') this._teardownWebRtc();
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
    const webrtc = this.transport === 'webrtc';
    switch (event.type) {
      case 'turn':
        this._responding = true;
        this._playbackPauseSeq = null;
        this._dropAck();
        if (webrtc) {
          // No per-chunk binary audio frames arrive over webrtc (assistant audio is a remote RTP
          // track — see _attachRemoteAudio), so app.js's `case "audio": if (client.responding && ...)
          // setState(SPEAKING)` would never fire. Rather than reinvent per-chunk detection (the
          // remote track already started/will start playing immediately), synthesize the one
          // 'audio' event apps actually key off of, right when a reply — and therefore audio — is
          // known to start. `samples` is null: there is no decoded PCM to hand a custom consumer.
          this._dispatch({ type: 'audio', sr: SAMPLE_RATE, samples: null, synthetic: true });
        }
        break;
      case 'playback_pause': {
        if (webrtc) break;   // not advertised as a capability over webrtc (see _buildStartFrame) — TODO
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
        if (webrtc) break;
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
        // Over webrtc the server owns playout and already stopped sending on its own retraction —
        // there is no local queue for this client to clear.
        if (!webrtc) this.player?.clear?.();
        this._responding = false;
        this._dropAck();
        break;
      case 'interrupted': // barged — stop the reply (fade-clear if the server asks, else drain).
        this._playbackPauseSeq = null;
        this._responding = false;
        this._dropAck();
        // Over webrtc the server measures and reports its own discarded audio on a hard-clear barge
        // (see broker_webrtc.py's WebRtcTransport.send_json) and there is no local player queue to
        // drain/fade — the wire contract explicitly forbids the client sending playback_stopped here
        // (note #5b), so webrtc takes none of the WS path's measurement/report below.
        if (webrtc) break;
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
