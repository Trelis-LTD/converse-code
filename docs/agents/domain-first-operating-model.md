# Domain-first operating model

Use the domain model to make valid behavior easy to express and invalid behavior hard or impossible to construct.

## Parse at boundaries

External data is untrusted and structurally unknown until parsed. JSON decoding is not parsing: decoded arrays, nulls, booleans, and objects with missing or wrong-typed fields are still outside the domain.

At each HTTP, WebSocket, file, environment, subprocess, or SDK boundary:

1. Decode once.
2. Parse into the smallest domain value that proves the required invariants.
3. Reject malformed input at that boundary with a protocol-appropriate error.
4. Pass only parsed values inward. Do not repeat `.get(...)`, truthiness checks, or defensive shape validation through the call graph.

Prefer a value that can exist only when valid over a primitive plus comments. Examples include a non-empty session ID rather than `str`, an immutable menu with at least one option and an in-range selection rather than `options + selected`, and a credential whose expiry is a positive integer rather than an arbitrary decoded mapping.

Coercion is not parsing. Turning an array, number, or object into a string makes malformed input look valid and destroys the evidence needed to reject it. Check the external type, normalize only within that type, and construct the domain value or fail at the boundary.

## Model states, not flags

When fields are meaningful only in certain phases, model those phases explicitly. Prefer tagged alternatives such as `Idle | Working | Canceling` over independent booleans and nullable fields that admit contradictions like “idle with an active request.” Store facts together when they must agree; do not represent a model name separately from the source that established it.

Transitions should consume one valid state and produce another. A canceled operation does not become idle when cancellation is requested; it becomes canceling until a matching completion or stable idle observation proves settlement. Derive serialized status from one observation so a repaint cannot produce mutually inconsistent fields.

Put data in the state where it is meaningful. Pending approvals belong to a running task, early events belong to a task awaiting acknowledgement, and delivered sequence IDs belong to a connected transport. Prefer one tagged state over a phase string accompanied by nullable IDs, terminal flags, failure strings, and cancellation booleans.

Required collaborators are part of the domain too. Supply handlers and lifecycle callbacks when constructing a component, or pass them explicitly with the operation that needs them. A started server with missing callbacks is an invalid application state, not an optional feature.

## Delete superseded representations

When a canonical representation replaces an old one, remove the old fields and consumers in the same change. Do not retain flattened aliases, always-empty queues, no-op relays, compatibility branches with no caller, or production knobs used only by tests. Git history is the migration record.

## Tests at the domain edge

Test parsing with invalid boundary shapes and valid examples. Test state transitions through events and observable outcomes. Avoid constructing contradictions by assigning private fields; that validates states production should not be able to create.
