# Deep research — try it out

Operator walkthrough for the research pipeline (`python -m research`): turn a
query into a cited markdown report using web search + your local models. How
it works internally: [deep-research.md](deep-research.md).

## Prerequisites

1. **The branch.** The code lives on the `deep-research` branch until merged:

   ```bash
   cd ~/git/rainbox && git checkout deep-research
   ```

2. **Research dependencies.** They live in their own pin file
   (`research/requirements.txt`) so they can't entangle the main app's
   dependency resolution, but they install into the same venv:

   ```bash
   cd source && venv/bin/pip install -r research/requirements.txt
   ```

3. **Postgres + a model provider running.** The CLI reads model groups from
   your normal RainBox database (`rainbox_production`), and the models run on
   whichever provider backs them — start LM Studio / Ollama / Jan as usual
   (see [operator-guide.md](operator-guide.md)).

4. **A model group named `research`.** Start the app (`python3 main.py`),
   open `/modelgroups`, and create a group named `research` with at least one
   member. Pick a model that handles **structured output** well — the
   splitter, query-gen, and URL-selection stages use it. Add a fallback
   member or two if you have them; the pipeline falls through the group in
   priority order on any failure, same as agent bindings. A different group
   works too via `--model-group <name>`.

5. **A search provider — optional.** DuckDuckGo works out of the box with no
   key (that's the zero-config default). For better results, export one of:

   ```bash
   export BRAVE_API_KEY=...        # api.search.brave.com — free tier ~2k queries/mo
   export SEARXNG_BASE_URL=http://localhost:8888   # your own SearXNG instance
   export FIRECRAWL_API_KEY=...    # firecrawl.dev — also unlocks --fetcher firecrawl
   ```

   `--search auto` (the default) picks the first configured of
   brave → searxng → firecrawl → ddg.

## Run it

```bash
cd ~/git/rainbox/source
venv/bin/python -m research "history of the diesel engine" --out /tmp/report.md
```

Progress streams to stderr while it works:

```
[setup] search=ddg fetcher=plain model_group=research
[plan] generating research plan
[split] splitting plan into subtasks
[split] 5 subtasks
[research] S1 Early development and patents
[fetch] skipped https://example.org/paywalled
...
[synthesize] writing executive summary
report written to /tmp/report.md
```

The report has an executive summary, one findings section per subtask with
`[n]` citations, an Open questions section (including anything that failed),
and a References list resolving every cited number to a URL.

Useful variations:

```bash
# quicker, smaller run
venv/bin/python -m research "compare pgvector index types" --max-subtasks 3

# force a specific search provider
venv/bin/python -m research "..." --search brave

# JS-heavy sites (needs FIRECRAWL_API_KEY)
venv/bin/python -m research "..." --fetcher firecrawl

# a different model group
venv/bin/python -m research "..." --model-group my-big-models

# slow reasoning model: give each LLM call up to 5 minutes
venv/bin/python -m research "..." --llm-timeout 300

# report to stdout instead of a file
venv/bin/python -m research "why do cats purr"
```

## Reading the claims ledger

With verification on (the default), `report.claims.jsonl` lands next to the
report: one row per source (quality tier), per claim (verdict, evidence,
action), per consistency conflict, and per open-question decision. This is
the audit trail — when a sentence in the report looks wrong, find its claim
row and see what the source actually said. `--no-verify` skips the whole
gate for a fast draft (roughly halves the LLM calls).

```bash
# claims that were corrected or dropped, with the source evidence
jq -r 'select(.event=="claim" and .action!="keep") | "\(.action)\t\(.text)\n  evidence: \(.evidence)"' report.claims.jsonl

# source quality tiers
jq -r 'select(.event=="source_tier") | "\(.tier)\t\(.url)"' report.claims.jsonl
```

## Reading the KPI log

With `--out report.md` a `report.events.jsonl` lands next to it (any run can
get one via `--events FILE`). First row = your model group's resolved
configs; middle rows = every LLM call (with per-model fallback attempts and
ms), search query, and fetch; last row = the summary. Quick looks:

```bash
# which model actually served calls, and who kept failing
jq -r 'select(.event=="summary") | .llm.models' report.events.jsonl

# every fallback: the member that failed, why, and after how long
# (member = group-member uuid — stable even when the same model name is in
#  the group twice with different settings; join it against the first row)
jq -r 'select(.event=="llm_call") | .attempts[] | select(.error) | "\(.member)\t\(.model)\t\(.ms)ms\t\(.error)"' report.events.jsonl

# is the search API the flaky part?
jq -r 'select(.event=="summary") | .search' report.events.jsonl

# time per pipeline stage
jq -r 'select(.event=="summary") | .llm.labels' report.events.jsonl
```

To compare models, run the same query twice with different `--model-group`
(or member order) and compare the two summary rows — served/error counts
and total ms per model tell you whether the fast model is doing the job.

## What to expect

- **Call volume:** with the default 5 subtasks and verification on, a run
  makes roughly 60–90 LLM calls (plan, split, per subtask: queries →
  selection → notes per source → findings; then per source: tier, per
  claim: entailment, plus consistency, rewrites, synthesis, and the
  open-question review). Subtasks run sequentially — one local GPU — so a
  run takes minutes, not seconds. `--max-subtasks 3` is a good first try;
  `--no-verify` halves the calls for a draft.
- **First call is slow** if the model isn't loaded yet — `prepare_llm` loads
  it into the provider on demand.
- **Failures degrade, they don't abort:** a rate-limited search, an
  unreachable page, or an irrelevant source is skipped and noted; only "every
  model in the group failed" stops the run.

## Troubleshooting

| symptom | fix |
|---------|-----|
| `error: model group 'research' not found; available groups: [...]` | Create it on `/modelgroups`, or pass `--model-group` with one from the list. |
| `error: model group 'research' has no members` | Add at least one model to the group on `/modelgroups`. |
| `error: search provider 'brave' is not configured` | Export the env var from step 5, or drop `--search brave`. |
| `error: all models in the research model group failed` | Provider not running, model missing, or context too small — test the member on `/model` first. |
| Empty or thin findings sections | DDG snippets can be weak; try `--search brave`, or a stronger model for the group. |
| `failed: timed out` / `structured stream exceeded 120s` on most calls | The model is slower than the timeout floor — raise it with `--llm-timeout 300`, or put a faster model first in the group. |
| Run feels stuck | Watch stderr — a `[research]`/`[fetch]` line tells you which subtask/URL it's on. Reasoning models spend a while per notes call. |

## Sanity checks without a model

No model group or key needed for these:

```bash
# the test suite (fakes throughout, no network, no LLM)
venv/bin/python -m pytest research/ -q

# clean startup error, proves wiring without any LLM/network work
venv/bin/python -m research "q" --search brave   # exits 1: not configured
```
