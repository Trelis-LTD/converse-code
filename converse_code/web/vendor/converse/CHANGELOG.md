# Changelog

## 0.10.0 - 2026-08-10

- Add a supported, versioned Browser SDK resume-state API for full page reloads:
  `resumeState`, `exportResumeState()`, `importResumeState()`, and the `resume_state` event. The
  state rotates with each server token and clears on terminal or rejected sessions, so apps can
  safely keep a tab-scoped `sessionStorage` copy without reading private SDK fields.
- Add `injectContext(text, {role, reply})` as the supported Browser SDK surface for the existing
  `inject_context` protocol frame, including proactive host announcements with `reply: true`.

## 0.9.0 - 2026-08-09

- Add `mode.silence_nudge_s` / `mode.silence_end_s` to override the broker's two-stage idle
  policy (check-in nudge, then sign-off + end) per session — useful for benchmark harnesses or
  flows with long think-time. Both default to the broker's env-configured values (10s/20s) when
  omitted; the broker also falls back to those defaults if either value is non-positive or
  `silence_end_s` does not exceed `silence_nudge_s`.

## 0.8.0 - 2026-08-08

- License Trelis-authored Browser SDK code under Apache 2.0; bundled AEC components remain under
  the third-party terms reproduced in the package.
- Keep assistant playback on the browser's unity-gain path, with no SDK limiter, software boost,
  output-route switch, or `navigator.audioSession` manipulation.
- Document that device volume, physical routing, and mobile full-duplex attenuation are controlled
  by the browser and operating system; native integration is required for routing guarantees.
- Document the Browser SDK support matrix across Chromium, Brave, Firefox and Safari, including
  the automatic WebKit fallback from experimental WebRTC to WebSocket.

## 0.7.0 - 2026-08-07

- Add `sendToolDeferred(id, {handle, statusLabel})` for jobs that outlive their originating voice
  turn. Eligible tools opt in with `deferred`, `deferred_timeout`, and `notify_on_complete`.
- Deferred jobs remain addressable by their original call ID or host handle for progress,
  cancellation, and exactly one terminal result. The SDK forwards `tool_deferred_ack` and
  `tool_deferred_resume` events through the normal typed and catch-all event APIs.
- Tool interruption is unchanged: barge-in never cancels deferred work; explicit cancellation does.

## 0.6.0 - 2026-08-06

- **Hidden-tab playback fix.** Browsers throttle `setInterval` to ~1 Hz in hidden tabs while the
  AudioContext keeps running, so the player's 0.2 s scheduling horizon starved playback into
  0.2 s bursts with ~0.8 s gaps whenever the user wasn't looking at the page. The player now
  commits a 2.5 s horizon while `document.visibilityState === 'hidden'` (and tops up immediately
  on `visibilitychange`), keeping playback gapless; the short barge-friendly horizon is unchanged
  in visible tabs, and barge/clear still stops committed-but-unplayed sources at any horizon.
- **Tool replies from anywhere.** `ConverseClient` gains `sendToolResult(id, content)`,
  `sendToolProgress(id, note)`, `sendToolPartialResult(id, content, {reply})` and
  `sendToolCancel(id)`, mirroring the Python SDK. Listen for `tool_call` events and answer them
  from the page or relay them to a backend — no raw-socket sidecar needed.
- `binaryToFloat32` and `toWebSocketUrl` are re-exported from the package root for raw-socket
  integrations.
- Server-side (deployed independently): the per-tool `timeout` ceiling rises from 120 s to 600 s
  for long-running agentic tools.

## 0.5.0 - 2026-08-06

- **Downlink audio is PCM16 on the wire — required update.** The server's downlink default
  changed from Float32 to PCM16 (bandwidth-halving negotiation, `start.audio.output_encoding`).
  0.4.5 and earlier decode the downlink as Float32 and therefore produce noise or decode errors
  against current servers. This release requests `output_encoding: "pcm16"` explicitly in the
  start frame, decodes Int16, and fails `connect()` loudly if the server's `ready.audio` field
  announces a format other than pcm16 at 16 kHz instead of playing garbage.
- `transport: 'webrtc'` option (experimental): carries the session over a WebRTC peer connection
  with a native remote audio track; the WS protocol remains the default and recommended path.
  Includes bounded ICE gathering, the `webrtc_connect_failed` error, and mic dropout/resampler
  fixes from the field-test rounds.

## 0.4.5 - 2026-07-30

- **Remove the speaker/earpiece output option entirely (`audioOutputMode`, `setAudioOutputMode`,
  and the 0.4.1 `<audio>`-element sink)** — settled by an 11-configuration on-device experiment
  (iPhone 17, Chrome + Safari): modern iOS routes web audio to the loudspeakers in every reachable
  configuration, "earpiece AND speaker at once" is normal iPhone stereo playback (the receiver is
  the second stereo speaker), and no configuration reaches earpiece-only. Web pages have no output
  routing to control, so the SDK ships none: playback is a plain
  `AudioContext` → master gain → `destination` path on every platform. The
  never-touch-`navigator.audioSession` regression guard remains.

## 0.4.4 - 2026-07-30

- **Revert 0.4.3's `navigator.audioSession` usage — field-falsified on real iPhones (Chrome and
  Safari).** Setting `type = 'playback'` (speaker mode) before capture made `getUserMedia` fail
  with a mic-permission error — a broken mic in the DEFAULT mode — and `'play-and-record'`
  (earpiece mode) still produced earpiece+loudspeaker dual output. Speaker routing is back on the
  0.4.2 `<audio>`-element sink (mic works; known-imperfect dual output remains the open bug). A
  regression test now pins that the SDK never touches `navigator.audioSession`.
  `setAudioOutputMode()` (live switching) and the speaker/earpiece option remain.

## 0.4.3 - 2026-07-30

- `ConverseClient.setAudioOutputMode(mode)`: the 0.4.2 speaker/earpiece choice is now live-switchable
  mid-session (previously took effect on the next `Start` only) — re-wires `StreamingPlayer`'s output
  route and, on WebKit, sets `navigator.audioSession.type` (`'playback'` for speaker, `'play-and-record'`
  for earpiece; Safari 16.4+, feature-detected, a no-op elsewhere). This directly targets the
  documented WebKit routing decision the `<audio>`-element sink workaround (0.4.1) could only work
  around indirectly — field testing on iPhone showed the 0.4.1 fix alone produced simultaneous
  earpiece + loudspeaker output rather than a clean switch, and `navigator.audioSession.type` is the
  platform's own API for this exact "mic is live, but I want loudspeaker anyway" case.
- The two mechanisms are mutually exclusive, not stacked: where `navigator.audioSession` exists
  (Safari 16.4+), the 0.4.1 `<audio>`-element sink route is skipped entirely (`hasAudioSessionApi()`
  in `aec.js`) — running both at once would leave two live output paths fighting over the same
  routing decision, the same shape that produced the dual-output field result. The element sink
  remains only as the pre-16.4 WebKit fallback.

## 0.4.2 - 2026-07-30

- Add the `audioOutputMode` (`'speaker' | 'earpiece'`, default `'speaker'`) `StreamingPlayer`
  constructor option so apps can explicitly choose between the loudspeaker route (0.4.1) and the
  platform's own call-audio/earpiece routing on iOS/WebKit, instead of only ever getting one. No
  effect on platforms without that fork (desktop, Android).

## 0.4.1 - 2026-07-30

- Route assistant playback through a sink `<audio>` element (instead of `AudioContext.destination`
  directly) on iOS/WebKit, to avoid the call-audio session routing playback to the earpiece
  receiver instead of the loudspeaker while a mic stream is active (WebKit bug 218012). Pending
  on-device confirmation.

## 0.4.0 - 2026-07-29

- Publish the package publicly on npm with installation and authentication guidance.

- Advertise reversible playback only for players that implement pause and resume, and report the
  actual SDK-owned microphone/AEC frontend across reconnects for echo diagnostics.
- Make web search opt-in by default.
- Add `setMicEnabled(enabled)` to gate SDK-owned microphone tracks without reopening capture.
- Add the default-on `playAcknowledgements` constructor option so half-duplex integrations can
  suppress automatic backchannel playback while retaining `ack` and `audio` event dispatch.
- Add `setVoice(voice)` to switch character voice from the next reply and reassert it on reconnect.
- Add `sendAmbienceState(active)` to report the client's ambience state on the session timeline.

## 0.3.1 - 2026-07-16

- Enable web search by default while preserving an explicit `webSearch: false` opt-out.

## 0.3.0 - 2026-07-16

- Add the opt-in `webSearch` session capability.

## 0.2.1 - 2026-07-15

- Gate `playback_pause_v1` to desktop Chromium and Firefox until physical WebKit validation.

## 0.2.0 - 2026-07-15

- Add synchronized processed/raw uplink framing for raw-assisted barge detection.
- Keep WebKit on one physical raw capture teed through SDK WASM AEC.
- Add reversible `playback_pause` / `playback_resume` handling for backchannels.
- Add AEC configuration plumbing and explicit desktop/WebKit engine controls.
- Fail closed to processed-only audio when raw capture or classification is unavailable.

## 0.1.0

- Initial browser SDK extracted from the Converse web client.
