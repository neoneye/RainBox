# Response variety for Q&A answers

**Status: proposal.** Let designated Q&A entries vary their replies —
different greeting every time, never the same phrasing twice in a row —
while everything not designated stays byte-for-byte consistent. The
flagship case is the greeting: `hi simon` should sometimes say "Hejsa",
sometimes "Olá — that's 'hi' in Portuguese", carry a live pulse of what's
going on, and offer up to three directions the conversation could take.
`uptime` keeps its fixed structure forever.

## Problem

Static Q&A entries return one canned string; asking the same thing twice
reads like a phone menu. The two existing "casual" handlers
(`get_status_casual`, `generate_joke` in `agents/query_handlers.py`) show
both the demand and the failure mode: they are memoryless
`random.choice(...)` over tiny pools, so they repeat immediately and can
serve the same joke twice in a row — randomness without memory is not
variety.

The requirement splits cleanly in two:

- **Consistent entries** (the default): live values with a fixed shape —
  uptime, git status, model info. Same structure every time is a feature.
- **Varying entries** (opt-in, per entry): greetings, small talk, jokes.
  Repetition is the bug; "have I said this recently?" is the missing state.

## Design

### 1. Opt-in per entry: `vary: true`

One new optional JSONL field. Absent (the default) means today's behavior,
untouched — the consistency guarantee for `uptime` is that the variation
machinery never runs for it.

```json
{"id": "…", "path": "smalltalk.greeting", "kind": "dynamic",
 "questions": ["hi simon", "hello", "hej", "good morning"],
 "handler": "get_greeting", "vary": true}
```

For **static** entries, `vary: true` additionally allows `answer` to be a
**list** — the cheapest variety source, no LLM, no handler:

```json
{"id": "…", "path": "smalltalk.thanks", "kind": "static", "vary": true,
 "answer": ["Anytime.", "You're welcome.", "Selvfølgelig."]}
```

### 2. The variety picker: recency memory, then choice

A small shared helper, `pick_varied(candidates, ctx)`, used by both
list-answers and vary-handlers:

1. **Recall what was said recently.** The agent's own recent replies in
   this room *are* the recently-used store — the transcript already
   persists them, per room, with timestamps, surviving restarts. Fetch the
   agent's last `K = 20` `kind="message"` rows (one indexed query on
   `chat_message`) and normalize them (the existing `_normalize_query`
   treatment: lowercase, collapsed whitespace).
2. **Filter.** Drop candidates whose normalized form appears in (or is a
   substring of) a recent reply. If everything is filtered — the pool is
   smaller than the window — drop the *oldest-used* first (recency aging,
   not permanent retirement).
3. **Pick** uniformly from the survivors (seedable RNG for tests).

**Why not a Bloom filter:** the candidate set and the history window are
both tiny (tens, not millions), a Bloom filter cannot delete — so a
greeting used once would be retired *forever*, the opposite of the aging
we want — and its false positives would silently shrink small pools.
Exact comparison over the last K messages is simpler, correct, and free at
this scale.

**"Too close", not just "identical" (phase 3):** for LLM-generated
candidates, exact matching misses paraphrases. There the picker upgrades
to embedding similarity — embed the candidate (`embeddinggemma:300m`, the
embedder already running for Q&A) and reject it when cosine similarity to
any recent reply exceeds a threshold (~0.90), regenerating once before
falling back to the deterministic pool. This is the "vector store
comparison" variant: no new store needed, just pairwise similarity against
K recent strings.

### 3. The greeting handler: `get_greeting`

A new dynamic handler composing three deterministic parts:

- **A greeting word** from a small data file
  (`data/greetings.jsonl`: `{word, language, gloss}` — "Hejsa" (da),
  "Olá" (pt), "Hoi" (nl), "Servus" (de-AT), …), chosen by the variety
  picker so the same word doesn't recur within the window. The
  **educational gloss** renders when that language hasn't appeared in the
  recent window: `Olá! (that's 'hi' in Portuguese)` — and is omitted when
  it has, so the lesson doesn't repeat either. Danish entries are
  first-class citizens of the pool, not the fallback.
- **A pulse** — one line probing current status, reusing the
  working-context sources (see the companion proposal): pending write
  intents awaiting confirmation, memory conflict candidates, in-progress
  kanban cards, the latest run digest. Cheap indexed reads, no LLM.
- **Up to three directions** the conversation can go, derived from the
  same probes, rendered as a short list:

```
Olá! (that's 'hi' in Portuguese)
Since yesterday: 1 edit proposal is waiting on you; board "website" has 2
cards in progress.
Want to (1) review the pending edit, (2) check the board, or (3) hear
what yesterday's runs did?
```

Each pulse/direction line carries its deep link (`/memory?id=…`,
`/kanban?id=…`) per the house rule that every influence is navigable.
Sections render only when non-empty — a quiet system greets with just the
greeting.

### 4. The optional LLM tier: higher temperature, checked output

For entries marked `vary: true`, an optional `vary_llm: true` field routes
the composed deterministic reply through one rephrase call using a
dedicated **"creative" model group** — model groups and per-override
`temperature` already exist (`ModelConfigOverride`; the synthesized labels
literally display `t0.9`), so "an LLM with a higher temperature setting"
is a binding, not new machinery. The output goes through the phase-3
too-close check; on failure or timeout the deterministic text ships as-is.

The LLM tier is deliberately last and optional: the picker + pools solve
the annoyance deterministically, testable without a model, and the
`query` agent (the no-LLM responder) still gets full variety.

### 5. Where it hooks in

`_resolve_match` (`memory/seed_memory.py`) is the single seam where a
matched entry becomes answer text — static answers and dynamic handlers
both pass through it. It grows the vary branch: list-answers go through
the picker; handlers receive a `QueryContext` that now includes the
recent-replies accessor. The assistant's `query_memory` observation path
is **excluded**: recalled facts feed reasoning, and varying them would
poison the "facts are stable" contract — variety applies only where the
resolved answer *is* the reply (the chat query agents).

Telemetry: the existing `debug-query` diagnostic row records which
candidate was chosen and which were rejected as recently-used, so
"why did it greet me in Portuguese?" is answerable.

## Phasing

1. **Picker + list answers.** `vary: true`, `answer` lists, the
   transcript-window picker; convert `get_status_casual` and
   `generate_joke` to it (they become regression tests: no immediate
   repeats). No LLM, no embeddings, no schema.
2. **`get_greeting`.** The greetings data file with glosses, the pulse +
   three directions over working-context sources, deep links.
3. **Too-close + creative tier.** Embedding similarity check;
   `vary_llm: true` with the creative model group; regenerate-once-then-
   fallback.

Acceptance: `uptime` output is byte-identical before/after every phase; a
seeded test greeting 10 times in a room produces 10 distinct greeting
words with each language glossed exactly once; emptying the pool recycles
oldest-first rather than failing; with the embedder down, phases 1–2
behavior is unchanged (the too-close check is additive).

## Rejected alternatives

- **Bloom filter** — no deletion (permanent retirement contradicts aging),
  false positives shrink small pools, and the scale never justifies it.
- **A new "recently used" table** — the room transcript already stores
  exactly this, per room, durably; a second store adds drift for nothing.
- **Vary everything by default** — consistency is the correct default for
  a knowledge base; variety is a per-entry editorial decision, like
  shields.
- **LLM-first variety** — unbounded cost on the greeting hot path, breaks
  the no-LLM `query` agent, and untestable without fakes; the LLM earns
  its place as a garnish on a deterministic base.

## See also

- `docs/qa-system.md` — entry schema and `_resolve_match`, the seam this
  extends.
- `2026-07-07-operator-profiles-and-working-context.md` — the pulse and
  the three directions reuse its working-context sources (and under a
  no-PII profile the greeting degrades to just the greeting word).
- `agents/query_handlers.py` (`get_status_casual`, `generate_joke`) — the
  memoryless prior art phase 1 upgrades.
- `docs/llm-providers.md` — model groups/overrides, where the
  high-temperature "creative" binding lives.
