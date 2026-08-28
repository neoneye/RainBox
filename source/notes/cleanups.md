# Cleanups

Known-but-deferred work: things that are wrong, stale, or untidy, and were
left that way deliberately rather than missed. Each entry says where it is and
why it was deferred, so picking one up does not start with an investigation.

This is a backlog, not a design doc. Delete an entry when it is done.

## Open

### `db.db.engine` and friends keep the doubled spelling

Seven occurrences: `db.db.engine` (3), `db.db.select` (3), `db.db.Model` (1).
`db.session` exists as a facade alias and `db.db.session` is gone, but
`engine` cannot have the same one — it is a property that requires an
application context, so binding it at import raises, whereas `session` is a
`scoped_session` registry proxy that resolves per context on each access.

Fixing `engine` means a function (`db.engine()`) or a module `__getattr__`,
either of which changes the call shape rather than just the name. Seven
call sites do not justify that yet. `select` and `Model` are one-liners
whenever someone wants them.

### A first turn that names a language is labelled `named_language`

`agents/response_language_gate.py` — the name check runs before the
no-previous check, so a room's very first message naming a language records
`trigger: "named_language"` where `no_previous` is equally true. Both ask, so
behaviour is right and only the trace label is arguable. The ordering is
deliberate — the name check is cheapest and independent of everything else —
so this is a labelling question, not a logic one.

## Not cleanups: decisions waiting on the operator

The response-language gate ships behind `assistant.response_language_gate`,
default off. Two things should happen before it is judged, both described at
the end of
`docs/superpowers/specs/2026-08-27-response-language-shift-gate-design.md`:
bind `assistant.response_language_classifier` to a small model on
`/agentmodel` (it resolves to `assistant.default`, which is why an 81-token
answer costs 11.4s), then turn the switch on and read some runs.
