# Converse Code engineering contract

Converse Code is a semantic bridge to Claude Code, not a general-purpose terminal remote.

## Interaction boundary

- Send ordinary user work to Claude Code as a natural-language `long_task`, or as `steer_task`
  guidance while that same episode is active.
- Keep direct TUI manipulation out of the public Converse tool manifest. The allowed narrow
  exceptions are managed cancellation, answering a currently visible blocking user-choice UI,
  and a semantic model change implemented with Claude Code's documented session-only
  `/model <alias>` command.
- Never use raw shell mode or bypass Claude Code's permission system.
- A menu may remain open only when it represents a real decision that requires the user. Surface
  its structured options and wait. Resolve deterministic internal confirmations inside the
  semantic operation that opened them, and fail closed on any unrecognized UI.
- Never type into Claude Code while an unrecognized blocking UI is visible.

## Evidence-backed outcomes

- Report only what the current invocation's observed evidence supports. Historical state or an
  earlier action is never evidence for a later call.
- Distinguish these milestones:
  - **accepted**: the matching `UserPromptSubmit` hook proves Claude Code accepted the prompt;
  - **completed**: the matching `Stop`/`StopFailure` proves that Claude Code's episode ended;
  - **verified**: an authoritative acknowledgement or observed state change proves the requested
    postcondition.
- Accepted is not completed. Completed is not automatically verified. Claude Code saying it
  opened an app, changed an external system, or produced another real-world effect is not
  independent verification of that effect.
- Set `verified: true` only when the current call has authoritative, target-specific evidence.
  Otherwise return the most accurate terminal outcome with `verified: false`, and phrase the
  response without upgrading uncertainty into success.
- Result text, `outcome`, `verified`, semantic state, and evidence must agree.

## Testing and release

- Regression tests must reproduce the real event order or screen boundary and assert a public
  outcome; substring-only tests are insufficient for lifecycle behavior.
- Use temporary project folders for agent, PTY, browser, and generated-app evaluations.
- Keep deterministic unit/browser/audio layers non-duplicative. Cost-bearing live harnesses sit
  above them and must have hard time, turn, and budget limits.
- Run an independent code review before live harnesses and again before publishing.
- Do not publish or deploy while deterministic tests, provenance checks, or release review have
  unresolved blockers.
