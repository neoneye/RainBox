# /benchmark_story — conversation benchmarks that exercise the prompt cache

Four benchmarks on their own page, in the shape of `/benchmark_basic`: a
table of targets × benchmarks, live polling, per-row Start. Each trial is a
five-turn conversation that writes a short piece a section at a time, to a
brief drawn at random from a list of 100.

## Why a conversation

The existing benchmarks are one-shot: one prompt, one answer, no history. A
prompt cache has nothing to work with there.

A multi-turn conversation is the opposite. Turn *n* resends the system
prompt and every earlier (user, assistant) pair, then appends one new user
message — so each turn's prompt is a strict **prefix extension** of the last
one. That is the ideal case for a KV cache, and it is what most of rainbox
actually does (the assistant's ReAct loop, chat, kanban workers).

So these benchmarks are two things at once: a capability test, and the
workload that puts a real, interpretable number on the /activity dashboard.
Expect near-total prefix reuse from turn 2 onward; anything less points at
the runtime evicting between calls.

## The four benchmarks

All four share one shape — system prompt, then five (user, assistant) rounds,
each asking for the next ~200-word section of the piece.

**The brief varies, the theme does not.** Each trial draws one topic from
`TOPICS`, sampled without replacement so three trials of a benchmark spend
themselves on three different pieces. A hundred briefs spanning narrative and
non-narrative, comic and bleak, and folklore from more than one part of the
world: an AI politician's stump speech, a layoff memo, a Black Mirror pitch,
Moby-Dick from the whale's side, a recipe that turns personal. A model that
only holds together on gothic horror should not be able to hide behind the
topic, and one sweep should leave a pile of unrelated pieces rather than a
dozen variations on one theme.

The topic is fixed for a whole trial, so the system message is byte-identical
on every turn — a prompt that varied per turn would break the very prefix the
suite exists to exercise.

| name | response | tools |
| --- | --- | --- |
| `story_text` | free text | — |
| `story_struct` | structured output | — |
| `story_text_tool` | free text | yes |
| `story_struct_tool` | structured output | yes |

**`story_text`** — the baseline. `llm.chat(messages)` each turn; the reply is
the section.

**`story_struct`** — `as_structured_llm(StorySection)` each turn.
`StorySection` has two fields: `section_text` (~200 words of the story) and
`section_reviewer`, a brutally harsh book-reviewer critique of that section.
The second field exists to make the schema carry two genuinely different
registers, so a model can't satisfy it by renaming its prose.

**`story_text_tool`** — a `FunctionAgent` with one tool, `omen_number()`,
returning a random integer. The section must contain that number. Correct
only if the number the tool returned appears verbatim in the section text,
which is checkable rather than a matter of taste, and proves the model
actually consumed the tool result instead of calling it and ignoring it.

**`story_struct_tool`** — the crossover: `FunctionAgent` with `output_cls=
StorySection` and the same tool. Verified working on the installed
llama_index (0.14.22): the result carries `.structured_response`, and
`chat_history` threads the conversation.

## Correctness

A trial is correct when all of:

- all five turns completed without an exception or timeout;
- every section is between 100 and 350 words (the ask is "around 200"; the
  band is wide enough that a model isn't failed for style, narrow enough
  that a one-line reply or a runaway wall of text is);
- structured variants: `section_reviewer` non-empty on every turn;
- tool variants: for every turn, the tool was called and the integer it
  returned appears in that turn's section text.

Trials that raise are `failures`; trials that complete but miss a criterion
are `mistakes`. Same three-way split the other suites use.

## Trial count

**Three**, not the five the other suites use. Each trial here is five LLM
calls, so three trials is fifteen calls per benchmark and 60 per target —
still several times the general suite's cost. Three also means three
different briefs per benchmark, which is the point of the topic list.

## The story artifact

Each trial's story is assembled into markdown and carried back to the page,
where a **Copy story** button puts it on the clipboard. Half the value of a
benchmark that writes fiction is reading the fiction.

Format: the brief as an `#` title — a piece pasted somewhere a week later
should say what it was asked to be — then `## Section N` headers with the
text beneath, the reviewer's verdict as a blockquote for the structured
variants, and the omen number in the heading for the tool variants.

This needs a new event on the worker protocol. Today the child emits only
`{correct, had_error, elapsed}` per trial; it gains
`{"t": "story", "bi", "trial", "text", "correct"}`, and the runner keeps the
texts on the benchmark entry so the polling page can offer them. Stories are
capped at 40 KB each so a runaway model can't grow the state dict without
bound.

## Components

**`benchmarks/story.py`** — the four benchmark classes over one shared
conversation driver, plus `StorySection` and the word-count/tool checks.
Same `run(on_trial, should_stop) -> BenchmarkResult` contract as every other
benchmark, so the runner needs no special case.

**`benchmarks/runner.py`** — `STORY_BENCHMARK_SPECS` and a `"story"` entry in
`SPEC_SETS`. Targets are collected as for the general suite, except that the
two tool benchmarks need a function-calling target; a target without the
capability fails those cells explicitly per trial rather than being skipped,
which is the existing convention.

**`benchmarks/worker.py`** — emits the new `story` event.

**`webapp/benchmark_views.py`** — `render_benchmark_page` gains an optional
`artifacts` flag; when set, a cell with stored stories renders a Copy button.
The other two pages pass nothing and are unchanged.

**`webapp/benchmark_story_views.py`** — the page, mirroring
`benchmark_kanban_views.py`: descriptions, intro, and the three endpoints.

**`webapp/core.py`** — a `story_benchmark_runner` instance and a nav entry
under the existing Benchmark dropdown.

## Testing

- **Word counting and the tool check** — pure functions, table-driven.
- **Story assembly** — the markdown an operator copies, including the
  structured variant's reviewer blockquotes and the empty-trial case.
- **The conversation driver** — a fake LLM that records the messages it is
  handed, asserting turn *n* receives exactly the previous history plus one
  new user message. This is the property the whole cache story rests on, so
  it is tested directly rather than inferred from a hit rate.
- **Correctness scoring** — a trial short of ten turns, a too-short section,
  a missing reviewer, a tool number absent from the text.
- **The page** — renders with no data, renders with stories, the Copy button
  appears only where a story exists, and the other two benchmark pages still
  render unchanged.
- **Live** — one real run against Ollama, confirming on /activity that the
  reusable-prefix rate is high, which is the whole point of the exercise.

## Risks

**Runtime.** 60 calls per target at ~10–20 s each is 10–20 minutes per
target. That is inherent to benchmarking conversations, not a defect, but
the page intro states it so nobody starts a full sweep unaware. Stop
SIGKILLs the child as it does today.

**Small models drift.** A 3B model asked for five sections may repeat itself
and drop the schema partway. That is a legitimate result —
the benchmark is measuring exactly that — but it means low scores on small
targets are expected and not a bug in the harness.
