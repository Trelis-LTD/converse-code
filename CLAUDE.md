# Converse Code engineering contract

Keep this package a small voice remote for the visible Pi terminal and a reference implementation
of Converse background tools and the Browser SDK. Pi/Codex owns coding; do not add terminal
emulation, screen parsing, model menus, generic keypresses, or provider-specific UI automation.

Voice requests must enter Pi through its documented extension `sendUserMessage()` API. Pi owns
the canonical coding transcript; the voice-only browser mirrors speech, replies, and tool activity.
Never navigate terminal menus. Blocking approvals must use the bridge's ID-correlated semantic
request/response and fail closed without an explicit matching user decision.

Preserve the public lifecycle: accepted prompt → deferred tool → bounded progress/partials → one
terminal result. Use `reply: true` only for information worth prompting an immediate conversational
response, especially blocking user input. Never report verification beyond current-call evidence.

Start non-trivial bug fixes with a regression test. Run Python through `uv`. Browser behavior must
be checked in real Chromium through `uv run scripts/browser_e2e.py`.
