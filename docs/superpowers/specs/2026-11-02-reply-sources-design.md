# Reply sources

A reply from the assistant carries the list of items it rests on — the stored
facts, diary passages, boards, tasks, web pages and files its reads turned up —
next to its text. The chat shows the list as a pill on the reply's action row,
and the next turn's prompt shows it under that reply, so a follow-up can read
an item again by its link instead of searching for it from scratch.

## Problem

A turn often reads a lot (a memory search, a board, a diary range) and answers
in two sentences. What it read is visible only on /assistant. The chat shows
the two sentences, and the next turn's prompt shows them too: a follow-up
("what else does it say about the chimney?") has nothing to go on but the
answer's words, so it searches again, and a paraphrased search may not find
what the first one did.

## Design

### The model names its sources; code checks every link

`reply` takes an optional `sources` argument alongside `message`:

```json
{"message": "...", "sources": [{"name": "...", "link": "..."}]}
```

- **link** — the item's uuid, URL or file path, copied exactly as an
  observation of this turn shows it: the item itself, never the step or the
  observation that showed it.
- **name** — a short title the model gives the item, in the language of the
  message.
- An empty list when the answer rests on no read.

The model picks, because only it knows which of twenty recalled facts the
answer used. Code does not trust what it picked: a link is kept only when its
text appears verbatim in an observation of this turn or in the request
message. Everything else is dropped. The model cannot invent a source, and an
answer from an earlier turn cannot be cited, since old answers are never
evidence (the existing turn-instructions rule).

A dropped source does not bounce the reply. A bounce costs a decide call, and
an unverifiable link is the one part of the reply code can repair by itself.
The drop is recorded on the reply's action row on /assistant, where it
happened, with the reason.

**Measured before building** (stored reply decisions of five recent runs
replayed on gemma4:e4b-mlx with the new listing):

- Replies that cited anything gave a uuid copied exactly 12 times out of 13.
- On a kanban run the model at first cited the step itself
  (`current_turn_steps.1.observation`) in 2 of 4 replies. With the "the item
  itself, never the step" clause, 10 of 12 replies cited only real uuids. Code
  drops the rest.
- One memory run answered from a general-knowledge-shaped fact and cited
  nothing in 3 of 4 replies. That is allowed.

### Each kept link is resolved by code

| Link | kind | page url |
|---|---|---|
| a full uuid | `db.describe_uuid` (one indexed lookup per table, no fuzzy or mention scans): its kind (`memory claim`, `Q&A entry`, `kanban task`, `cron job`, …), else `record` | the match's url, else `/find?q=<uuid>` |
| `http://` / `https://` | `web` | the link |
| anything else | `path` | none |

The link itself stays as written (a bare uuid, not `rainbox:<uuid>`): a
follow-up passes it straight to `memory_query {"uuid": ...}` or `kanban_read`,
and the kind is a field of its own, so a prefix would only have to be stripped
again. Names come from the model; an empty name falls back to the resolved
record's name, then to the link.

Limits: at most 20 sources (`MAX_REPLY_SOURCES`), a name cut at 120
characters, a link longer than 500 characters dropped, duplicate links merged.

### Storage

`chat_message.meta["sources"]` on the reply: `[{"name", "link", "kind",
"url"}]`. No schema change. The reply step's observation records the same
list plus the dropped entries (`data.sources`, `data.dropped_sources`), and
its text lists both, so the trace shows them.

### Follow-ups

`conversation_history_xml` renders an earlier reply's sources as a sibling of
its message, the same adjacency `message_summary_markdown` uses:

```xml
<message role="assistant" timestamp="...">...</message>
<message_sources>
  <source kind="memory claim" link="...">name</source>
</message_sources>
```

A sibling, not a line appended to the message text: a reply that ends in a
source list teaches the model to write source lists into its replies.

The turn instructions gain one paragraph, next to the rule that old answers are
never evidence: a `message_sources` section lists what that earlier answer
rested on. Its items are places to look, not facts: to use one, read it again
this turn, by its link, with the action that reads that kind of item.

### Chat UI

- **The pill.** A reply with sources gets a pill at the end of its action row,
  on the same line as copy, feedback and the ⋯ menu: "1 source" or
  "N sources".
- **The Sources panel.** Clicking the pill opens the right sidebar's new
  **Sources** mode for that reply. The panel is a numbered list. Each item
  shows its name, linked to the page url in a new tab when it has one. Under
  the name it shows the kind and the link, in monospace and selectable.
- **Without a chosen reply.** The panel shows the newest reply in view that
  has sources. This covers picking Sources from the dropdown and a reload
  with the mode remembered.
- **Room change.** Switching rooms forgets the chosen reply.

## Out of scope

- Writes are not sources. A write's link is still appended to the reply text
  (`_append_result_links`).
- Discord and Telegram bridges relay the reply text only. Sources stay in the
  web chat.
- Clarifying questions carry no sources.

## Tests

- **Agents:**
  - Normalizing the argument: dicts, bare strings, a non-list, duplicates,
    caps.
  - Keeping links seen in an observation or in the request, and dropping
    anything else, with its reason.
  - Resolving a uuid to its kind and page, falling back to /find.
  - A URL and a path.
  - A loop run whose reply cites one fact posts `meta.sources` and settles the
    reply step with sources and drops.
  - The history renders `message_sources` after an earlier reply.
  - The reply listing documents `sources`.
- **Webapp:** markers for the pill, the Sources mode option, the panel
  renderer, and the room-switch reset.
- **Live:** one assistant turn on the sandbox core with a stored fact. The
  reply carries the fact's uuid, the pill shows it, and the panel lists it.
