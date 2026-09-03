# A cache view for the assistant's prompts

## Problem

Each model call on `/assistant` carries a `cached N` KPI, but a count says how
much of the prompt the runtime reused, not *where* the reuse stopped. Finding
the point at which a 50,000-character prompt diverged from the previous call
means reading two prompts side by side, and the divergence is what has to be
fixed — either in rainbox's prompt assembly or in the runtime's cache.

The data to place it already exists. Every `llm_call` row records two prefix
lengths over the prompt, and they mean different things:

- `cached_tokens` — estimated from prefill timing against the model's cold
  baseline. What the runtime evidently served from cache. NULL while the model
  is still calibrating.
- `reusable_prefix_tokens` — exact, from hashing the flattened prompt in
  1,000-character blocks against recent prompts to the same model. What the
  runtime *could* have reused.

On run `28fc6f1d`, the second decide call had 12,182 reusable tokens of
12,482 and cached about 4,900: the prompt was almost entirely reusable and the
runtime dropped most of it. The first decide call had 6,072 reusable, so
there rainbox's own assembly had broken the prefix. Two causes, two fixes, one
number on the page.

Both counts are prefixes of the same flattened text, `<system>` + system prompt
+ newline + `<user>` + user prompt (`llm.activity.prompt_text`). The step's
stored `system_prompt` and `user_prompt` are byte-identical to the message
contents that were hashed (verified on that run), so a prefix length over the
flattened text maps to one split point in each prompt pane.

## Design

### The mode selector

The Inspect card header gets a two-button switch, `normal | cache`, styled
like the existing raw/pretty switch (`.ev-view`). The choice is one preference
for the page, held in a JS variable and persisted to localStorage under
`assistant.mode`, read once at load. Persistence follows the JSON view's rule:
storage failing must not stop the switch from working. More modes are
appended as more buttons and more branches of `applyInspectMode`; nothing
else has to change.

### What the server adds

`_apply_call_kpis` puts `reusable_tokens` on the event beside `cached_tokens`.
It is not added to `_KPI_FIELDS`, so the meta line and the markdown export are
unchanged.

The flattening format moves to `llm.activity_metrics` as `flatten_prompt` and
`content_spans`, both pure; `llm.activity.prompt_text` calls the first. The
page and the recorder read the layout from one place, so a boundary the page
draws is on the text that was hashed.

The system-prompt and user-prompt blocks of an `llm` event carry a `cache`
field when the event has `input_tokens` and at least one of the two prefix
counts. Token counts are scaled onto the flattened prompt's length
proportionally (the same scaling `reusable_prefix_tokens` used to get from
characters to tokens), then clipped to each pane's own span:

```
cache = {
  "cached":   chars of THIS pane inside the cached prefix, or None,
  "reusable": chars of THIS pane inside the reusable prefix, or None,
  "cached_tokens": ..., "reusable_tokens": ..., "prompt_tokens": ...
}
```

`_block_html` emits it as one `data-cache` JSON attribute on the `<pre>`. A
block without the field emits nothing, and the markdown export reads blocks,
not attributes, so it never sees any of this.

### What the page draws

`applyInspectMode(root)` runs at load, after every mode switch, and after the
live refresh swaps the pane (beside `applyJsonView`). In `cache` mode, for
every `pre.ev-pre[data-cache]`:

1. Keep the recorded text once in `dataset.raw` (the JSON view's convention).
2. Replace the content with up to three spans over that same text:
   - `.cc-hit` — `[0, cached)` — green: served from cache.
   - `.cc-miss` — `[cached, max(cached, reusable))` — amber: reusable, not
     cached. The runtime's loss.
   - `.cc-new` — the rest — red: not reusable. Rainbox's divergence.
   A missing `cached` (calibrating) draws no green band and the legend says
   so; a missing `reusable` draws no amber band.
3. Insert one legend line under the block naming the three counts in tokens.

`normal` mode restores `dataset.raw` and removes the legend. `textContent` is
identical in both modes, so the copy button copies the same bytes and the
line clamp measures the same height.

Resolution is proportional scaling, accurate to roughly a paragraph, which is
also the resolution of the 1,000-character hashing. The legend states the
counts; the colours say where.

## Out of scope

- A per-model tokenizer for exact boundaries.
- Any change to what is recorded, or to the `/activity` page.
- Painting responses, reasoning or rejected attempts; only the two prompt
  panes are prefixes the cache reads.

## Testing

- `llm/test_activity_metrics.py`: `content_spans` slices of `flatten_prompt`
  return each content unchanged; `prompt_text` still produces the same string.
- `db/test_assistant_log.py`: the call join attaches `reusable_tokens`.
- `webapp/test_assistant_components.py`: prompt blocks carry `data-cache` with
  the expected offsets — a prefix ending inside the system prompt leaves the
  user prompt at zero; a prefix past the system prompt fills it; no counts, no
  attribute; the markdown export is unchanged.
- `webapp/test_assistant_views.py`: the page carries the switch, the storage
  key, and applies the mode after the live swap (marker tests, as the JSON
  view has).
