# Browser and virtual-audio testing

Converse Code has a real-Chromium behavioral suite in addition to its JavaScript unit tests. It
loads the packaged HTML and modules from `LocalServer`, opens the authenticated WebSocket, grants
microphone permission, and drives the controls as a user would. The Converse SDK entry module is
replaced by a deterministic in-browser fake, so this tests the shipped page and browser media
permissions—not the SDK's WebSocket/audio pipeline—and does not require hosted credentials.

## Install and run

```bash
uv sync
uv run playwright install chromium
uv run scripts/browser_e2e.py
uv run scripts/audio_loopback_probe.py
```

Chromium receives a generated WAV file through its fake microphone device. The suite verifies page
boot, terminal-link state, typed-turn canonical echoes and streaming replies, microphone lifecycle,
mute/unmute, stopping voice while preserving the session, text reuse, injected status rendering,
and bridge acknowledgements.

On failure, the suite writes a full-page screenshot and Playwright trace under
`test-results/browser/<test-name>/`. Open the trace with:

```bash
uv run playwright show-trace test-results/browser/<test-name>/trace.zip
```

## Virtual output loopback on Linux

`audio_loopback_probe.py` starts an isolated PulseAudio server, creates a null sink, launches headed
Chromium under Xvfb when no display exists, records its monitor, and rejects silent output. It neither
uses nor changes the user's default audio server. A successful run leaves the captured WAV at
`test-results/browser-audio/loopback.wav` for inspection.

BlackHole is a macOS driver. On Linux, use a PulseAudio-compatible null sink (native PulseAudio or
PipeWire's PulseAudio layer). Every null sink has a monitor source, so browser output can be recorded
without physical speakers:

```bash
pactl load-module module-null-sink sink_name=converse_code_test \
  sink_properties=device.description=ConverseCodeTest
PULSE_SINK=converse_code_test chromium ...
ffmpeg -f pulse -i converse_code_test.monitor browser-output.wav
```

Keep this loopback opt-in: it depends on host audio services and is suitable for nightly/live
diagnostics, not deterministic unit CI. Do not route the monitor back into the browser microphone
unless intentionally testing echo cancellation or barge-in, or the test creates an audio feedback
loop.

The fake-microphone suite proves browser media permissions and capture lifecycle, but not physical
room acoustics, hardware routing, or Safari/iOS behavior. Those remain device tests.
