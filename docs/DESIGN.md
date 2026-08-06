# Converse Code — Design Spec

Based on the public Converse tool-calling contract, documented at
`converse.trelis.com/docs/api/websocket/#tools`, plus the Claude Code driver mechanics
worked out in design discussion. Everything here builds against that documented protocol
alone.

## 1. What this is

`converse-code` is a single CLI that lets a dev talk to a running Claude Code session by
voice. It wraps the real interactive `claude` CLI in the dev's own terminal, connects to
Converse over the documented WebSocket protocol, and exposes the Claude Code session as a
set of callable tools. Converse's voice brain, ASR, TTS, turn-taking, and barge-in are all
unchanged and untouched — `converse-code` is just another client speaking the documented
contract.

Nothing about voice ever reaches Claude Code directly, and nothing about Claude Code's raw
output ever reaches the user directly. `converse-code` is the translation layer in both
directions.

## 2. Developer experience

```
uvx converse-code          # in your project directory
```

- **First run**: prompts for a Converse API key (from the converse.trelis.com dashboard),
  validates it with the free `{"type": "auth"}` frame, and stores it in the OS keychain
  (fallback: `~/.config/converse-code/`). `CONVERSE_API_KEY` overrides for headless setups.
  Claude Code auth is separate and already solved: the wrapped `claude` is the dev's own
  interactive CLI, so their normal subscription login just works.
- **Every run**: Claude Code opens in the terminal exactly as if the dev had run `claude`
  directly — same TUI, fully typeable — and `converse-code` prints (and offers to open)
  `http://localhost:<port>`: a browser tab with a mic button, speaker output, and a live
  view of the conversation.
- The dev talks into the tab. Instructions appear in the terminal as typed text; Claude
  Code works; the voice narrates what happened. Closing the terminal stops everything.

**What shows in the terminal is not a transcript of the speech — by design.** Speech goes
to the Converse brain, and what lands in the terminal is the brain's *formulated
instruction* (the tool call's `request` argument): rambly speech compressed to one crisp
instruction, corrections collapsed, and conversational turns ("how's it going?") never
touching the terminal at all. Two guardrails keep this trustworthy: the browser tab shows
the live ASR transcript alongside what was actually sent (so heard-vs-sent drift is
visible immediately), and the tool description prose instructs the brain to preserve the
user's technical wording — exact names, flags, phrasings — compressing filler rather than
editorializing. For precision-critical input, the dev just types into the terminal
directly; voice for intent, keyboard for precision.

## 3. Components

```
   Dev's terminal                        Dev's browser tab (localhost)
   `claude` TUI, fully interactive      mic + speaker + live ASR transcript
        ^                                       ^
        | pty (keystrokes in,                   | audio in/out + captions
        |      rendered screen out)             | (local WebSocket)
        +------------------+-------------------+
                           |
                    converse-code   <- this repo
                           |
                           |  ONE Converse WebSocket carrying both the audio
                           |  stream and the tool protocol (start / tool_call /
                           |  tool_result / tool_progress / tool_partial_result /
                           |  tool_cancel)
                           v
              Converse broker (unchanged)
         brain, ASR, TTS, turn-taking, barge-in
```

The single-socket shape is load-bearing: the documented protocol carries a session's audio
and its tool declarations on one connection, so the mic audio must reach the process that
holds the tools. The localhost browser tab is the simplest way to do that — no separate
app, no pairing. (Remote/phone use later is just opening that same page over Tailscale;
see Section 11.)

## 4. Tool manifest (sent in the `start` frame)

Following the documented shape exactly:

- **`long_task`** -- `requires_permission: true`, `timeout: 120` (the protocol's current
  hard cap -- see Section 8 below). Description field carries the *guidance prose* the brain
  uses to decide when to call it, including the preserve-the-user's-technical-wording
  instruction from Section 2.
- **`stop_long_task`** -- fast tool, no permission gate needed (interrupting is generally
  lower-risk than starting), used only when the *brain* judges the user actually wants work
  stopped (never fired on a bare barge -- barge only stops speech, per Section 4 of the protocol).
  Its description prose should state plainly: this loses unfinished work, don't use it for
  progress questions or added instructions, ask if ambiguous.
- **`check_status`** *(optional, recommended)* -- `read_only: true`. Lets the broker fire it
  speculatively/early for "how's it going?" without waiting for turn commit. Returns
  whatever the last progress checkpoint said plus the current screen state (see Section 10),
  cheaply, without touching the CC session.
- **`command`** -- fast. Takes a raw slash-command string (`/clear`, `/model`, `/compact`,
  ...) and injects it verbatim. One generic tool, not one tool per command: Claude Code's
  command set is open-ended (skills, plugins, project commands), so hardcoding a manifest
  entry per command would go stale immediately. The description prose names the handful of
  common ones and says any `/command` the user asks for can be passed through; whatever UI
  the command opens is handled by the state machine (Section 10). Optionally paired with a
  `read_only` `list_commands` that reads the available commands so the brain can answer
  "what can it do" -- deferrable until asked for.
- **`select_option`** -- fast. Used when the screen is in a menu state (Section 10): takes
  the chosen option's text, and `converse-code` translates it into the right arrow-key
  presses + Enter itself. The brain never counts keystrokes.
- **`press_key`** -- fast, low-level fallback: one of a small enum (escape, up, down, enter,
  tab, ctrl-c, shift-tab). For anything the higher-level tools didn't anticipate. Its
  description should steer the brain toward the high-level tools first.

## 5. Claude Code driver mechanics

- **Session**: `converse-code` spawns the real interactive `claude` binary on a pty and
  acts as a transparent wrapper in the dev's own terminal -- dev keystrokes pass straight
  through, the TUI renders normally, and nothing looks different from running `claude`
  directly. Not a hidden pty, and not `claude -p`/bare mode (bare mode drops subscription
  auth per Anthropic's docs; the interactive CLI keeps the dev's normal login). One
  `converse-code` process = one Claude Code session = one project directory.
- **Input**: on `tool_call` for `long_task`, translate `args.request` into a single
  instruction and write it (plus Enter) to the pty. This is indistinguishable from real
  typing -- the dev watches it appear live in their terminal.
- **Output**: don't screen-scrape ANSI for CC's replies. Install a `Stop` hook (fires when
  Claude finishes a turn) that signals `converse-code`, then read the actual JSONL
  transcript Claude Code already writes to disk for that session (`transcript_path`,
  handed to hooks) to get clean structured text of what Claude said -- no ANSI, no spinner
  noise, and it distinguishes prose from tool calls/diffs, which raw terminal text can't.
- **Screen snapshots**: the pty output is additionally fed through an in-process terminal
  emulator buffer (e.g. `pyte`) on its way to the real terminal, so `converse-code` can
  snapshot the *rendered screen* at any moment. This is used only for structure -- menu and
  queue detection (Section 10) -- never as the source of CC's prose.
- **Permission prompts inside CC itself**: Claude Code's own tool-approval prompts are a
  distinct case from the Converse-level `requires_permission` gate -- install a
  `PermissionRequest` hook, surface it as a `tool_progress` note or hold the call open, and
  only auto-approve if you've deliberately decided that's safe for this host (default:
  surface it, don't auto-approve).
- **Stop**: `stop_long_task` maps to writing Escape to the pty -- that is Claude Code's
  turn-interrupt. Not Ctrl-C (which clears the input line, or exits on a double press) and
  never killing the process -- matches "the host adapter owns the mechanism" language in
  Section 4 of the protocol docs.

## 6. Result shape (strict, per protocol Section 2)

```json
{"speak": "Edited auth.py and two tests -- suite passes.",
 "data": {"files": ["auth.py", "tests/test_auth.py"], "tests": "passed"},
 "handle": "cc-session-<id>"}
```

- `speak`: short natural-language summary only -- never a diff, log, or transcript. The
  brain narrates this in its own words on the next turn; it is not spoken verbatim (that's
  the brain's job, not `converse-code`'s).
- `data`: small structured breadcrumbs.
- `handle`: the Claude Code session id, so a follow-up `long_task` call continues the
  *same* CC session/context rather than starting cold -- CC's session holds the verbose
  stuff; the brain's context stays thin.
- Hard cap: 16 KiB compact JSON at the boundary -- oversized results get truncated by the
  broker with a marker, so keep summaries genuinely short.

## 7. Progress while CC works

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

## 8. Constraints to design around now, not later

- **120s timeout ceiling**: `timeout` in the tool manifest is still hard-capped at 120s.
  (The underlying STT-recycling blocker has been fixed broker-side -- rotation now happens
  concurrently with an in-flight tool call -- but the cap is deliberately unchanged while
  that mechanism soaks in production.) A real coding task can easily exceed 120s, so the
  pattern is: hold `long_task` open for up to ~110s streaming partial results, resolve
  early when the Stop hook fires, and otherwise resolve at the deadline with the `handle`
  and a "still working" status so the brain can issue a follow-up call against the same
  session. Note the in-flight call is also `converse-code`'s *only* push channel to the
  brain -- there is no host-initiated message outside a call -- which is the other reason to
  keep one open while CC works rather than resolving instantly and forcing the brain to
  poll.
- **No stop keyword-matching** -- stopping is a judgement call made by the brain from
  context, expressed via the tool call, never via string-matching "stop" anywhere in
  `converse-code`.
- **A barge never cancels tool work** -- CC keeps running across a barge; only an explicit
  `stop_long_task` call interrupts it.

## 9. What enters the voice brain's context

`converse-code` fully controls what the brain ever sees of Claude Code, and the design
goal is a *thin* brain context: CC's own session (reachable via `handle`) is the durable,
verbose memory; the brain holds only enough to converse about the work. Everything that
crosses the boundary, exhaustively:

- the tool manifest descriptions (static prose, written once)
- `speak` fields -- one or two sentences each
- `data` -- state, queue, files touched, test outcome, last milestone; small named facts
- `tool_progress` notes -- short free-text checkpoints

Never: raw transcripts, diffs, logs, file contents, or CC's tool-call dumps. The 16 KiB
terminal / 2 KiB partial caps enforce a ceiling, but the target is far below them --
per-result payloads in the low hundreds of bytes, because every byte lands in the brain's
context permanently and voice conversations are long-lived.

When the user genuinely wants detail ("what exactly did it change?"), the answer is not to
ship traces -- it's a follow-up `long_task` asking CC itself to summarize ("describe what
you changed in two sentences"). CC already holds the full context, is good at compressing
it, and the answer comes back through the same thin `speak` channel.

## 10. TUI state machine

Most of the tools are instant keystroke/text injections -- the long-running thing is
Claude Code itself, and Section 8's hold-open pattern covers that. What `converse-code`
must get right instead is *state*: several slash commands (`/model`, `/config`),
permission prompts, and trust dialogs open interactive selectors that swallow the next
keystrokes, so blindly writing a new instruction to the pty would type it into a menu.

Minimum viable states:

- **idle** -- prompt ready, safe to inject text or slash commands
- **working** -- CC is mid-turn (between input injection and the Stop hook firing)
- **menu** -- an interactive selector or dialog is open (model picker, permission prompt,
  trust dialog, ...)

Detection: the JSONL transcript + Stop hook cover idle<->working, but menus never touch the
transcript -- detect them from the screen-buffer snapshots (Section 5; menus have a stable
visual signature: an option list with a selection cursor). This is the one place
screen-reading is unavoidable, and it's reading for *structure* (which options exist,
which is highlighted), not for CC's prose output.

Every tool result and partial carries `data.state`, so the brain always knows what it's
talking to. When a menu opens, `converse-code` parses the visible options from the screen
buffer and returns them in `data` so the brain can read them out; selection then goes
through `select_option` (high-level, by option text) with `press_key` as the low-level
fallback (Section 4). If a `long_task` arrives while state is `menu`, `converse-code`
refuses it with a result explaining what's on screen instead of typing into the menu.

**The queue is part of state.** Claude Code natively queues input submitted while it's
mid-turn (queued messages render below the input box), so a `long_task` arriving in
`working` state is *not* an error -- `converse-code` injects it and immediately reports
"queued behind the current task" as a partial. But the brain must be able to see that
queue, or it can't explain why nothing is coming back quickly. Queued items live only on
screen, never in the transcript, so they're read from the same screen-buffer snapshots as
menus, and `data.queue` (the list of pending queued instructions) rides along with
`data.state` on every result, partial, and `check_status` response.

## 11. Later, not now

- **Detach/persistence**: running the wrapped session inside tmux (invisibly, managed by
  `converse-code`) so it survives closing the terminal. Opt-in later; the pty wrapper is
  the default because it requires nothing of the dev.
- **Remote/phone use**: serve the same browser page over Tailscale instead of localhost --
  same page, different bind address, plus ttyd for a remote terminal view. No app.
- **Pairing with official Converse clients**: if the broker later supports a second
  connection joining an existing session (mic on one device, tools on another),
  `converse-code` drops its audio relay and pairs with the real Converse app. Until that
  exists in the public protocol, the localhost tab is the audio path.

## 12. Open questions

- Whether `check_status` is worth building now or deferred until real usage shows the brain
  needs it.
- Exact policy for CC's own internal permission prompts (surface-and-wait vs. scoped
  auto-approve list).
- Whether the browser tab needs a text-input fallback (type instead of talk) for noisy
  environments, or whether the terminal already covers that.
