# Go operating model

This is the default change workflow for Converse Code. Its purpose is confidence per line of code, not maximum test count.

## Start from behavior

State the user, protocol, security, or operational behavior being protected. Read the domain model and trace the real boundary-to-outcome path before choosing a seam. A test must explain which regression it catches and why a cheaper existing test does not already catch it.

A strong test supplies a real boundary event and observes a public outcome: an HTTP status, wire frame, delivered control, rendered terminal result, persisted artifact, security rejection, or state transition. Regression tests should reproduce the failure mechanism, not copy the fix.

## Tests must justify their maintenance cost

Delete or rewrite tests that:

- read source, HTML, documentation, or configuration and assert literal strings;
- restate constants, generated manifests, private dictionaries, or exact helper call sequences;
- require edits whenever a harmless implementation detail changes;
- duplicate a stronger integration or boundary test;
- manufacture impossible collaborators or mutate private state;
- merely call a function without asserting an observable result.

Exact values remain appropriate when they are the external contract itself, such as a wire encoding, security permission, or documented protocol limit. Assert them at the boundary where a consumer observes them, not by importing the constant from both sides.

## Simplify after deleting tests

Every test removal triggers a production search for the seam it kept alive. Remove test-only flags, constructor parameters, accessors, callbacks, branches, and data fields. Then follow the removal through every serializer and UI consumer. Do not leave permanent empty values or adapters for hypothetical callers.

## Parse, then operate

Follow `docs/agents/domain-first-operating-model.md`. Boundary code parses; domain code operates on valid values. Prefer immutable values and explicit state alternatives. If a bug arises from two fields disagreeing, replace the pair with one representation instead of adding another validator.

## Verification ladder

Run the narrowest behavioral tests while developing, then the deterministic suite. Review the final diff for unnecessary seams, state contradictions, security boundary regressions, and deleted coverage. A green suite is necessary but not sufficient: inspect what the remaining tests prove.

This repository audit established the practical baseline: real PTY, local HTTP/WebSocket, browser SDK codec, credential, reconnect, cancellation, and security tests earn their place; source scans, duplicated constant checks, private-state snapshots, and test-only production modes do not.
