# Vendored `@trelis/converse` browser SDK

Copied verbatim from npm `@trelis/converse@0.6.0` (`package/src/`). Do not edit
these files — re-vendor from npm to update, and bump the version noted here.

Why vendored rather than a CDN or an npm dependency: the voice tab is served by
a Python CLI with no build step and must work offline, so the modules are
shipped as static files under this directory.

Why used at all: this SDK owns microphone capture, echo cancellation, streaming
playback and interruption handling. Hand-rolling that produced a string of
audio defects (a Float32/PCM16 decode error, chunk-seam clicks, no jitter
buffer); the SDK is the same code the Converse playground runs.

`package.json` is retained for the version/licence record. The package is
published `UNLICENSED` — it is used here as first-party Trelis code.

Version history that matters here, all found the hard way:
- 0.4.5 decoded the downlink as `pcm_f32le` after the server default became
  PCM16 — pure noise against a live server.
- 0.5.0 fixed that, and fails `connect()` loudly if the server announces an
  unexpected format instead of playing garbage.
- 0.6.0 fixed hidden-tab playback (a 2.5s scheduling horizon while the tab is
  hidden, replacing a workaround this page used to carry) and added
  `sendToolResult`/`sendToolProgress`/`sendToolPartialResult`/`sendToolCancel`.

`tests/web_audio_check.mjs` pins both: the page must not reintroduce its own
audio handling, and the vendored player must contain the hidden-tab fix.
