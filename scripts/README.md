# Diagnostics

Run with `uv run scripts/<name>.py` from the repo root. These talk to real
services, so they are kept out of the test suite.

- `smoke_real_claude.py` — drives the real `claude` CLI through the tool router:
  injects an instruction, waits for the Stop hook, prints the spoken summary and
  state. Catches everything a fake TUI cannot (transcript timing, hook payloads,
  screen quirks).
- `vendor_converse_sdk.py` — regenerates the static browser SDK from an extracted,
  Apache-licensed `sdk/browser` source tree, pins its upstream commit, and carries
  its license, NOTICE, changelog, and third-party terms into the wheel.
