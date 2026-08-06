# Converse Code — Design Spec

Based on the public Converse tool-calling contract, documented at
`converse.trelis.com/docs/api/websocket/#tools`, plus the Claude Code driver mechanics
worked out in design discussion. Everything here builds against that documented protocol
alone.

## 1. What this is

A standalone process — **the Bridge** — that connects to Converse over the existing
WebSocket tool protocol and exposes a running Claude Code CLI session as a callable tool.
Converse's voice brain, ASR, TTS, turn-taking, and barge-in are all unchanged and untouched
— the Bridge is just another client speaking the documented tool contract. (In the
protocol's own terminology, the Bridge implements the "tool host" role; that term won't be
used again here since it's internal jargon, not something an end user needs.)

Nothing about voice ever reaches Claude Code directly, and nothing about Claude Code's raw
output ever reaches the user directly. The Bridge is the translation layer in both
directions.

## 2. Components

```
User (voice/text)
     |
     v
Converse broker (unchanged) -- brain, ASR, TTS, turn-taking, barge-in
     |  WebSocket tool protocol (start/tool_call/tool_result/tool_progress/tool_cancel)
     v
Claude Code Bridge   <- what you're building
     |  tmux send-keys (input) / Stop-hook + JSONL transcript tail (output)
     v
tmux session running the real `claude` CLI
     |
     v (dev can `tmux attach` locally, or view via ttyd/Tailscale from phone)
Dev's terminal(s)
```

## 3. Tool manifest (sent in the `start` frame)

Following the documented shape exactly:

- **`long_task`** -- `requires_permission: true`, `timeout: 120` (the protocol's current
  hard cap -- see Section 7 below). Description field carries the *guidance prose* the brain uses
  to decide when to call it.
- **`stop_long_task`** -- fast tool, no permission gate needed (interrupting is generally
  lower-risk than starting), used only when the *brain* judges the user actually wants work
  stopped (never fired on a bare barge -- barge only stops speech, per Section 4 of the protocol).
  Its description prose should state plainly: this loses unfinished work, don't use it for
  progress questions or added instructions, ask if ambiguous.
- **`check_status`** *(optional, recommended)* -- `read_only: true`. Lets the broker fire it
  speculatively/early for "how's it going?" without waiting for turn commit. Returns
  whatever the last progress checkpoint said plus the current pane state (see Section 10),
  cheaply, without touching the CC session.
- **`select_option`** -- fast. Used when the pane is in a menu state (Section 10): takes the
  chosen option's text, and the Bridge translates it into the right arrow-key presses +
  Enter itself. The brain never counts keystrokes.
- **`press_key`** -- fast, low-level fallback: one of a small enum (escape, up, down, enter,
  tab, ctrl-c, shift-tab). For anything the higher-level tools didn't anticipate. Its
  description should steer the brain toward the high-level tools first.

## 4. Claude Code driver mechanics

- **Session**: one long-lived tmux session per coding project/conversation, running the
  real interactive `claude` binary -- not a hidden pty, not `claude -p`/bare mode (bare
  mode drops subscription auth per Anthropic's docs; the interactive CLI in a real tmux
  session keeps the dev's normal login).
- **Input**: on `tool_call` for `long_task`, translate `args.request` into a single
  instruction and inject it via `tmux send-keys "<text>" Enter`. This is indistinguishable
  from real typing -- the dev's own `tmux attach` shows it appear live.
- **Output**: don't screen-scrape ANSI. Install a `Stop` hook (fires when Claude finishes a
  turn) that signals the Bridge, then read the actual JSONL transcript Claude Code already
  writes to disk for that session (`transcript_path`, handed to hooks) to get clean
  structured text of what Claude said -- no ANSI, no spinner noise, and it distinguishes
  prose from tool calls/diffs, which raw terminal text can't.
- **Permission prompts inside CC itself**: Claude Code's own tool-approval prompts are a
  distinct case from the Converse-level `requires_permission` gate -- install a
  `PermissionRequest` hook, surface it as a `tool_progress` note or hold the call open, and
  only auto-approve if you've deliberately decided that's safe for this host (default:
  surface it, don't auto-approve).
- **Stop**: `stop_long_task` maps to sending Escape in the tmux pane -- that is Claude
  Code's turn-interrupt. Not Ctrl-C (which clears the input line, or exits on a double
  press) and never killing the process -- matches "the host adapter owns the mechanism"
  language in Section 4 of the protocol docs.

## 5. Result shape (strict, per protocol Section 2)

```json
{"speak": "Edited auth.py and two tests -- suite passes.",
 "data": {"files": ["auth.py", "tests/test_auth.py"], "tests": "passed"},
 "handle": "cc-session-<id>"}
```

- `speak`: short natural-language summary only -- never a diff, log, or transcript. The
  brain narrates this in its own words on the next turn; it is not spoken verbatim (that's
  the brain's job, not the Bridge's).
- `data`: small structured breadcrumbs.
- `handle`: the tmux session name/id, so a follow-up `long_task` call can resume the *same*
  CC session/context rather than starting cold -- the Bridge's own context (CC's session)
  holds the verbose stuff; the brain's context stays thin.
- Hard cap: 16 KiB compact JSON at the boundary -- oversized results get truncated by the
  broker with a marker, so keep summaries genuinely short.

## 6. Progress while CC works

Two channels exist while a call is in flight (protocol docs Sections 3/3a); neither
interrupts playback and neither resolves the call:

- **`tool_progress`** -- free-text note, <=500 chars, <=12 per call. Available if the user
  asks ("how's it going?"), otherwise silent.
- **`tool_partial_result`** -- structured partial in the same `{speak, data, handle}` shape
  as a terminal result, capped at 2 KiB each / 8 per call (much smaller than the 16 KiB
  terminal cap -- never push diffs or logs through it). Optional `reply: true` makes the
  broker proactively narrate that milestone right away, best-effort. Use `reply: true`
  sparingly -- one or two genuine milestones per task ("tests passing", "done") -- and leave
  it false (the default, surfaced on the next natural turn) for everything else.

Pull checkpoints from the transcript tail, not raw tool-call dumps. Don't try to stream
every line -- pick the moments worth mentioning.

Barge/orphan semantics (protocol Section 4) are unchanged: a barge never cancels tool work,
progress and partials keep landing after a barge, and a late terminal result still lands in
context whenever it arrives.

## 7. Constraints to design around now, not later

- **120s timeout ceiling**: `timeout` in the tool manifest is still hard-capped at 120s.
  (The underlying STT-recycling blocker has been fixed broker-side -- rotation now happens
  concurrently with an in-flight tool call -- but the cap is deliberately unchanged while
  that mechanism soaks in production.) A real coding task can easily exceed 120s, so the
  pattern is: hold `long_task` open for up to ~110s streaming partial results, resolve
  early when the Stop hook fires, and otherwise resolve at the deadline with the `handle`
  and a "still working" status so the brain can issue a follow-up call against the same
  session. Note the in-flight call is also the Bridge's *only* push channel to the brain
  -- there is no host-initiated message outside a call -- which is the other reason to keep
  one open while CC works rather than resolving instantly and forcing the brain to poll.
- **No stop keyword-matching** -- stopping is a judgement call made by the brain from
  context, expressed via the tool call, never via string-matching "stop" anywhere in the
  Bridge.
- **A barge never cancels tool work** -- CC keeps running in its tmux session across a
  barge; only an explicit `stop_long_task` call interrupts it.

## 8. Dev-visibility UX

The dev attaches to the same tmux session locally (`tmux attach -t <handle>`) and watches
everything the Bridge does in real time -- voice-triggered instructions appear as typed
text, CC's replies stream normally. They can also type manually into the same session at
any time; nothing about the Bridge claims exclusive control of the terminal. For
remote/phone viewing, a ttyd-over-Tailscale setup (bound to the tailnet IP, basic-auth
protected) attaches as a second read/write viewer of the same tmux session -- no separate
mechanism needed.

## 9. TUI state machine

Most of the Bridge's tools are instant keystroke/text injections -- the long-running thing
is Claude Code itself, and Section 7's hold-open pattern covers that. What the Bridge must
get right instead is *state*: several slash commands (`/model`, `/config`), permission
prompts, and trust dialogs open interactive selectors that swallow the next keystrokes, so
blindly `send-keys`ing a new instruction would type it into a menu.

Minimum viable states:

- **idle** -- prompt ready, safe to inject text or slash commands
- **working** -- CC is mid-turn (between input injection and the Stop hook firing)
- **menu** -- an interactive selector or dialog is open (model picker, permission prompt,
  trust dialog, ...)

Detection: the JSONL transcript + Stop hook cover idle<->working, but menus never touch the
transcript -- detect them from `tmux capture-pane` snapshots (menus have a stable visual
signature: an option list with a selection cursor). This is the one place screen-scraping
is unavoidable, and it's scraping for *structure* (which options exist, which is
highlighted), not for CC's prose output.

Every tool result and partial carries `data.state`, so the brain always knows what it's
talking to. When a menu opens, the Bridge parses the visible options from the pane capture
and returns them in `data` so the brain can read them out; selection then goes through
`select_option` (high-level, by option text) with `press_key` as the low-level fallback
(Section 3). If a `long_task` arrives while state is `menu`, the Bridge refuses it with a
result explaining what's on screen instead of typing into the menu.

## 10. Open questions for the new repo

- Whether `check_status` is worth building now or deferred until real usage shows the brain
  needs it.
- Exact policy for CC's own internal permission prompts (surface-and-wait vs. scoped
  auto-approve list).
- Multi-project support: one tmux session per project, keyed by `handle`, or one global
  session.
