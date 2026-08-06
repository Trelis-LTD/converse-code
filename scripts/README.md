# Diagnostics

Run with `uv run scripts/<name>.py` from the repo root. These talk to real
services, so they are kept out of the test suite.

- `smoke_real_claude.py` — drives the real `claude` CLI through the tool router:
  injects an instruction, waits for the Stop hook, prints the spoken summary and
  state. Catches everything a fake TUI cannot (transcript timing, hook payloads,
  screen quirks).
- `probe_tool_limits.py` — asks production what tool `timeout` values it accepts.
  Used to confirm the ceiling moved from 120s to 600s rather than trusting a
  changelog; re-run it if the manifest is ever rejected with `invalid_tools`.
