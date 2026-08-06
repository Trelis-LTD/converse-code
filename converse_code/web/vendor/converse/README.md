# Vendored `@trelis/converse` browser SDK

Copied verbatim from npm `@trelis/converse@0.4.5` (`package/src/`). Do not edit
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
