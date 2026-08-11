# Diagnostics

Run with `uv run scripts/<name>.py` from the repo root. These talk to real
services, so they are kept out of the test suite.

- `evals.py` — the single entry point for non-overlapping validation layers. `quick` runs
  deterministic tests, `pr` adds real Chromium, `release` adds the real Converse Code PTY
  lifecycle, and `extended` adds the separately scoped hosted/SDK/audio probes.
- `smoke_agent_sdk.py` — an upstream Agent SDK compatibility diagnostic. It reuses one SDK
  session and one repo-local temporary project for file operations, acknowledged model switching,
  game creation, code review, and real-Chromium behavior; it does not exercise Converse Code.
- `eval_voice_text.py` — a real hosted Converse/Claude Code tool-loop evaluation using text turns
  in place of audio.
- `probe_converse_session.py` — opens one bounded production session, reports admission, and closes
  it; use it to diagnose account/session-capacity failures.

- `smoke_real_claude.py` — drives the real `claude` CLI through the tool router:
  injects an instruction, waits for the Stop hook, prints the spoken summary and
  state. Catches everything a fake TUI cannot (transcript timing, hook payloads,
  screen quirks).
- `browser_e2e.py` — generates deterministic microphone audio and drives the shipped browser UI in
  real Chromium. Failures retain a screenshot and Playwright trace under `test-results/browser/`.
- `audio_loopback_probe.py` — a generic host-output diagnostic: it launches headed Chromium (using
  Xvfb when needed) against an isolated PulseAudio null sink, records its monitor, and verifies an
  oscillator emitted a non-silent signal. It does not exercise the Converse page or SDK.
- `vendor_converse_sdk.py` — regenerates the static browser SDK from an extracted,
  Apache-licensed `sdk/browser` source tree, pins its upstream commit, and carries
  its license, NOTICE, changelog, and third-party terms into the wheel.
