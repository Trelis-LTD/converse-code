# Converse Code

A deliberately small voice remote for a normal, visible Pi terminal, and a reference
implementation for [Converse](https://converse.trelis.com) background tools and the Browser SDK.
Pi uses the user's ChatGPT Plus/Pro Codex subscription.

The example demonstrates the complete deferred-tool lifecycle:

1. `coding_task` accepts work and immediately backgrounds it.
2. Pi tool events become progress and partial results.
3. Routine edits use silent partials; meaningful milestones use `reply: true`.
4. `continue_task` steers the running task from a later voice turn.
5. Pi's `agent_settled` event resolves the deferred tool exactly once.
6. Converse cancellation maps directly to Pi's extension `abort()` API.

The browser remains voice-only but mirrors the live speech transcript, assistant replies, and
coding activity. Pi retains the canonical coding transcript and model state. A bundled extension
injects voice requests with Pi's documented `sendUserMessage()` API and observes Pi's semantic
lifecycle events. There is no terminal emulation, screen scraping, key injection, model picker,
shell bypass, or hidden Pi process.

That same extension gates `bash`, `edit`, and `write` with ID-correlated semantic approval
requests. Converse asks the user to allow once, allow for the session, or block, and Pi accepts
only an explicit response for the pending ID. Approval narration is queued until any active voice
reply finishes and is acknowledged by the Browser SDK. No terminal selection menu is opened or
navigated.

## Install

Install Pi, sign into the Codex provider, and install Converse Code:

```bash
npm install -g @earendil-works/pi-coding-agent
pi
# In Pi: /login → ChatGPT Plus/Pro (Codex)

uvx converse-code
```

`converse-code` opens the voice page and then starts a normal interactive Pi TUI as:

```bash
pi --provider openai-codex
```

Override that command when testing another Pi configuration:

```bash
converse-code --pi "pi --provider openai-codex --model gpt-5.6-codex"
```

Run `converse-code login` to store a Converse API key. The persistent key remains in Python; the
browser receives only a short-lived session credential.

## Reference architecture

```text
Converse voice model
        │ tool call / cancellation
        ▼
Voice-only Browser SDK page
        │ acknowledged localhost controls
        ▼
AgentToolRouter ── local semantic bridge ── visible Pi TUI ── Codex
        │
        └─ deferred / progress / partial(reply) / terminal result
```

The model-facing surface is intentionally limited to `coding_task`, `continue_task`,
`approval_decision`, and `end_session`. See [docs/DESIGN.md](docs/DESIGN.md) for the event mapping
and evidence rules.

## Test

```bash
uv sync
uv run pytest -q
uv run playwright install chromium
uv run scripts/browser_e2e.py
```

The deterministic suite exercises the Python bridge and the real TypeScript Pi extensions. The
browser suite drives the shipped voice-only page in Chromium and checks transcript streaming,
activity indicators, backgrounding, silent and spoken partials, completion, cancellation, and
reconnect replay. Release testing also launches a real visible Pi TUI and injects a bounded task
through the semantic extension bridge.

## License

Apache-2.0. The vendored Converse Browser SDK retains its own notices and third-party licenses.
