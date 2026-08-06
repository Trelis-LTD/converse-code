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
  whatever the last `tool_progress` note said, cheaply, without touching the CC session.

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
- **Stop**: `stop_long_task` maps to the Ctrl-C equivalent in the tmux pane (send the
  interrupt keystroke), not killing the process -- matches "the host adapter owns the
  mechanism" language in Section 4.

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

Send `tool_progress` notes (<=500 chars, <=12 per call) at meaningful checkpoints -- e.g.
"running the test suite," "editing auth.py" -- pulled from the transcript tail, not raw
tool-call dumps. These don't interrupt playback and don't resolve the call; the brain
narrates them naturally only if asked ("how's it going?"). Don't try to stream every line --
pick the moments worth mentioning.

## 7. Constraints to design around now, not later

- **120s timeout ceiling**: the protocol caps tool timeout at 120s because the broker can't
  yet recycle its STT stream while awaiting a tool (documented as the top item to fix before
  "genuinely long tools"). A real coding task can easily exceed this. Until that's fixed
  broker-side, design `long_task` to return *fast* with progress notes and a `handle`, and
  let the brain issue a follow-up call to check on / continue the same session rather than
  blocking one call for the full task duration.
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

## 9. Open questions for the new repo

- Whether `check_status` is worth building now or deferred until real usage shows the brain
  needs it.
- Exact policy for CC's own internal permission prompts (surface-and-wait vs. scoped
  auto-approve list).
- Multi-project support: one tmux session per project, keyed by `handle`, or one global
  session.
