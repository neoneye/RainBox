# S8 — unify chat agents onto hybrid retrieval + profile block — design (2026-06-20)

**Status:** approved-direction, complete spec (decisions all made; implement
directly). Implements card **S8** of
[`../../proposals/2026-06-20-improvements-v3.md`](../../proposals/2026-06-20-improvements-v3.md):
move the two chat agents off the legacy token-overlap `retrieve_memories` onto
`retrieve_memories_hybrid` (vector + full-text + entity), and give them the
**user-profile block** — the biggest remaining recall win, finishing the Phase 3
story (the assistant already uses both).

## Decisions (made, with rationale)

- **Chat retrieval switches to `retrieve_memories_hybrid`**, inside the existing
  shared `build_chat_memory_block`. Hybrid degrades to lexical when no embedder is
  present, so it is a strict recall improvement over token-overlap (stemmed
  full-text + entity + vector). Hard filters (secret/expired/scope) are the same
  shared `hard_filtered_claims`, so the safety contract is unchanged.
- **Suppress hybrid's own telemetry on the chat path** (`record_telemetry=False`,
  new param). `build_chat_memory_block` keeps its existing
  `chat_memory_retrieval` telemetry (`retrieved` + `used`) so the relevance
  dashboard is unchanged — only the retrieval *algorithm* changes, not the
  telemetry contract.
- **Chat agents get the profile block**, injected **before** the memory block
  (same order as the assistant). Added at the **agent layer** via a new shared
  `agents/chat_context.py` helper — `user_profile` imports `memory.retrieval`, so
  putting the profile block inside `memory.retrieval` would be a cycle; the agent
  layer imports both freely. Best-effort: a profile failure never breaks a turn.
- **`retrieve_memories` (legacy token-overlap) is kept**, now test-only in
  production terms. Removing it cascades to its tests for no functional gain;
  noted as a removable follow-up.
- **Contradiction surfacing is DEFERRED to S12.** v2 Phase 3 wanted read-only
  detection of conflicting claims ("lives in NYC" vs "moved to SF"). It is a
  distinct feature from this retrieval migration; bundling it would bloat S8. The
  decision is recorded (this is the card's explicit "build or defer" call).

## `memory/retrieval.py`

`retrieve_memories_hybrid` gains a telemetry toggle:

```python
def retrieve_memories_hybrid(
    query, *, agent_uuid, room_uuid, limit=6, include_secret=False,
    journal_id=None, embed_fn=None, record_telemetry: bool = True,
) -> list[RetrievedMemory]:
    ...
    # in the result loop, guard the existing telemetry write:
        if record_telemetry:
            try:
                db.record_retrieval_event(... source="memory.hybrid" ...)
            except Exception:
                logger.warning("memory: failed to record hybrid retrieval telemetry")
```

`build_chat_memory_block` switches its retrieval call (keeps its own telemetry):

```python
    memories = retrieve_memories_hybrid(
        query, agent_uuid=agent_uuid, room_uuid=room_uuid,
        limit=retrieval_limit, include_secret=include_secret,
        journal_id=journal_id, record_telemetry=False,
    )
```

(unchanged: the latest-human-message query extraction, `_record_memory_telemetry`,
`format_memory_context`, and the `(memory_block, query, memories)` return shape.)

## `agents/chat_context.py` (new)

```python
"""Assemble a chat agent's memory context: the operator profile block (active
self-model) followed by hybrid memory retrieval. Lives in the agent layer because
user_profile imports memory.retrieval (so memory.retrieval can't import it)."""

import logging
import memory.retrieval as memory_retrieval
import user_profile

logger = logging.getLogger(__name__)


def build_chat_context_block(messages, *, agent_uuid, room_uuid, journal_id=None):
    """Return (context_block, query, memories): the profile block (if any) then
    the hybrid memory block, joined by blank lines. Best-effort on the profile."""
    memory_block, query, memories = memory_retrieval.build_chat_memory_block(
        messages, agent_uuid=agent_uuid, room_uuid=room_uuid, journal_id=journal_id,
    )
    profile_block = ""
    try:
        profile_block, _ = user_profile.build_profile_block(
            agent_uuid=agent_uuid, room_uuid=room_uuid, journal_id=journal_id,
        )
    except Exception:
        logger.warning("chat: profile block failed", exc_info=True)
    parts = [b for b in (profile_block, memory_block) if b]
    return "\n\n".join(parts), query, memories
```

## `agents/chat_structured.py` and `agents/chat_unstructured.py`

In each `user_prompt`, replace the `memory_retrieval.build_chat_memory_block(...)`
call + assembly with the shared helper:

```python
    from agents.chat_context import build_chat_context_block
    context_block, query, memories = build_chat_context_block(
        messages, agent_uuid=self.agent_uuid, room_uuid=room_uuid, journal_id=journal_id,
    )
    self._last_retrieval_query = query
    self._last_retrieved_memories = memories
    transcript = format_history(messages)
    return f"{context_block}\n\n{transcript}" if context_block else transcript
```

(`_last_retrieved_memories` still drives the `debug-memory` row in `handle` —
unchanged. The profile block is prompt-only, not part of `debug-memory`.)

## Tests (TDD, model-free)

**`agents/test_chat_context.py` (new):**
1. **hybrid recall on the chat path:** a claim that stemmed full-text matches but
   exact token-overlap misses (e.g. "running marathons" vs query "marathon run")
   is now in `build_chat_memory_block`'s result — proving the upgrade. Use a fake
   embedder that raises (lexical/full-text only), mirroring `test_hybrid_retrieval`.
2. **profile + memory combine:** `build_chat_context_block` returns a block with
   both the profile digest (an active preference) and the memory block, profile
   first.
3. **secret/expired still filtered** on the chat path (hybrid hard filter).

**`agents/test_chat_memory.py` (extend):**
4. **profile block in the chat prompt:** with an active `preference` claim, a
   `StructuredChatAgent.user_prompt(...)` contains "About the operator" before the
   "Relevant remembered facts" block.

## Done when

- Both chat agents retrieve via hybrid and carry the profile block (profile before
  memory before transcript).
- A stemmed-full-text-only match is retrieved on the chat path (recall gain);
  secret/expired/out-of-scope claims are still filtered.
- Chat telemetry (`chat_memory_retrieval`) is unchanged (no double-recording).
- Model-free tests cover the above; full affected suite green.

## Out of scope (follow-ups)

- Read-only contradiction surfacing (→ S12).
- Removing the now-test-only `retrieve_memories` (→ S12).
- A recall eval harness comparing hybrid vs token-overlap numerically (the tests
  prove the specific full-text-recall gain; a broad eval is separate).
