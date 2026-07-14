# Deep research — design

`python -m research "query"` turns a research query into a cited markdown
report: web search + page fetching + local LLMs. Standalone package
(`research/`, no imports from `agents/`); the public seam is
`research.pipeline.run_deep_research(query, config, progress_cb)`, which
chat/kanban/cron integrations can call with a custom progress callback.
Full design rationale: `docs/superpowers/specs/2026-07-07-deep-research-design.md`
(repo root).

## Pipeline

Deterministic Python control flow; the LLM is called only at judgment
points, each call small enough for a modest local context window:

0. **Scope** (`scope.py`) — one structured call disambiguates the query.
   Hypothetical framing ("a (hypothetical or upcoming) film") is scrubbed
   from the chosen scope in code — the prompt ban alone was ignored, and a
   hypothetical scope poisons every downstream stage
   (many terms name a standard, a connector, a product line, and a software
   component at once): plausible meanings, a chosen scope, exclusions. The
   rendered scope block travels in the user message of every later stage so
   selection and notes can reject keyword-only matches, and the report
   opens with a Scope section so the reader sees which interpretation they
   got.
1. **Planner** (`planner.py`) — query + scope → research plan (plain text).
2. **Splitter** (`splitter.py`) — plan → 3–8 subtasks (structured); ids and
   the `max_subtasks` cap are assigned in Python.
3. **Researcher** (`researcher.py`, per subtask, sequential — one GPU):
   generate 2–4 search queries (structured) → search → select which results
   to read by index (structured) → fetch + extract → per-source notes
   (plain, one source per call) → findings section citing `[n]` (plain).
4. **Corpus recovery** (`researcher.recover_subtask_from_corpus`) — a
   failed subtask gets a second chance against the run's own corpus: the
   registry keeps every source's raw extract, and a fact fetched for one
   subtask often answers another (notes are subtask-scoped and discard the
   rest). Selection + fresh notes run against the stored extracts; only if
   that also fails does the subtask stay failed.
5. **Verifier** (`verifier.py`, on by default; `--no-verify` skips) — the
   gate between "cited" and "checked". Classifies each source's quality
   tier (official/reference/encyclopedia/news/blog/marketing/tabloid, shown
   in References), extracts the checkable claims per findings section,
   checks every claim against the RAW extracts of the sources it cites
   (never the notes — a compression checked against a compression lets
   amplified errors through), runs one consistency pass across all
   surviving claims (an entity acting before it existed, a trend stated in
   both directions), then rewrites each section from the verdicts:
   keep / correct / hedge / drop. Unsupported claims backed only by
   blog/marketing/tabloid sources are dropped; a section with nothing left
   is marked failed. Claims carry a **mode**: a supported interpretation
   (a critic's reading, an analogy) may appear only in attributed form
   ("one commentary reads X as ..."), never stated as fact. Echoed
   claim-action lines are stripped from rewrites in code — verifier
   machinery never reaches the reader. The framing layer is claims too:
   the executive summary goes through the same extract/entail/rewrite gate
   (synthesis must not reintroduce dropped facts), and the chosen scope
   statement is checked against the fetched corpus and REPLACED with a
   source-grounded restatement whenever the sources don't support it
   (acting only on contradiction let an "unsupported" scope calling a real
   film hypothetical reach the header) — before the open-question review
   runs, which consumes the corrected scope — a run once kept asserting a wrong film year in the
   Scope header after the body verifier had dropped that same claim. When
   at least half the classified sources are blog/marketing/tabloid, the
   report carries a deterministic source-quality caveat under Scope. Every
   decision lands in the claims ledger (`report.claims.jsonl`, `--claims`)
   — the prose is the view, the ledger is the audit trail.
6. **Interpretation** — when the scope stage detects that the query asks
   an analytical question (how does X relate to Y?), a dedicated stage
   answers it from the verified material as an explicitly-labeled
   "## Interpretation" section: analysis without overclaiming — never
   presented as creators' intent or established fact. Fact retrieval alone
   would answer such a query with "not found", which is safe but useless.
7. **Synthesizer** (`synthesizer.py`) — findings → executive summary + open
   questions (two plain calls). Synthesis input degrades instead of
   aborting: a body within the char cap can still overflow a small model's
   context window (empty replies, timeouts), so failed calls retry with
   first-paragraphs-only bodies at decreasing caps before the model-group
   error propagates; the interpretation stage shrinks the same way and is
   skipped entirely — with a progress note — rather than killing a
   finished run. Findings sections land in the report
   verbatim; synthesis can't lose detail. A deterministic sweep then moves
   any stray interrogative lines (a model echoing its instructions as
   prose) from findings/summary into Open questions, and the verifier
   reviews each open question against the corrected scope and verified
   claims — a question a verified claim already answers, or that
   manufactures doubt about something the sources state plainly, is
   removed or narrowed — and then tries to ANSWER the survivors from the
   run's own corpus: a question whose answer sits in an already-fetched
   page becomes a "Resolved: ..." bullet instead of a false gap.

Sources get run-wide citation ids via `SourceRegistry`, which keeps notes
AND raw extracts; a URL fetched for an earlier subtask is not refetched —
its notes are reused, and its extract feeds corpus recovery. A notes call
that returns nothing from a 4000+-char extract is retried once (the empty
reply contradicts the fetch itself). Failed searches, fetches, and subtasks
never abort the run; they surface under Open questions. Language is chained
explicitly: plan in the query's language, subtasks in the plan's, findings
in the subtask's — mixed-language reports were a real failure mode.

## Search providers

`websearch.py` defines the `SearchProvider` protocol and registry
(mirroring `providers/registry.py`). Env-configured:

| id        | needs               |
|-----------|---------------------|
| brave     | `BRAVE_API_KEY`     |
| searxng   | `SEARXNG_BASE_URL` (instance must enable the JSON format) |
| firecrawl | `FIRECRAWL_API_KEY` |
| ddg       | nothing (`ddgs` library) |

`--search auto` (default) picks the first configured in that order (ddg
last: keyless but rate-limity). Fetching (`fetch.py`) is `requests` +
`trafilatura` with a 2 MB / 20 s / 8000-char cap chain and an SSRF guard
(non-public IPs refused, re-checked on every redirect hop; DNS rebinding
between check and connect remains a known limitation); `--fetcher firecrawl`
scrapes via Firecrawl's API for JS-heavy pages.

## Models

`caller.py` resolves a **model group** (default: the group named
`research`; create it on the /model page) and falls through its members in
priority order on any failure — the same contract as agent model bindings.
Machine-readable stages use structured outputs; prose stages use plain
chat. Research calls run longer than chat calls, so every member's resolved
timeout gets a **floor** (default 120 s, CLI `--llm-timeout`,
`ResearchConfig.llm_timeout_s`): a chat-tuned 60 s config is raised to the
floor, a configured value above it is kept.

## Prompt-injection posture

- `prompts.py` holds only constant system prompts — no interpolation
  (enforced by `test_research_prompts.py`). The user query and all
  web-derived text travel in user messages.
- Page text is wrapped in `BEGIN/END UNTRUSTED SOURCE [n]` blocks with the
  delimiters defanged inside the body; prompts instruct models to treat the
  blocks as data.
- Models pick search results by **index**, never by URL — a hallucinated or
  injected URL cannot reach the fetcher. Control flow is Python, so injected
  text can at worst poison one source's notes, which citations make
  auditable.

## Testing

`venv/bin/python -m pytest research/ -q` — fake callers and fake search
providers throughout; provider parsing runs against recorded JSON fixtures
in `research/fixtures/`; no live network or LLM.

## Telemetry (KPIs)

`telemetry.py` — a JSONL event stream for assessing model/provider
trade-offs. The CLI writes it next to `--out` (`report.md` →
`report.events.jsonl`; override with `--events`); library callers pass a
`Telemetry` sink to `run_deep_research`. Rows, in order:

- `run` (first): query, full config, and every group member's **resolved
  settings** (provider, model, arguments incl. context window and timeout
  overrides) in fallback order, each with its `member` uuid.
- `llm_call`: stage label (scope/plan/split/queries/select/notes/
  findings/summary/open_questions), `served_by` (the group-**member** uuid — the
  stable identity, since one model name can sit in a group several times
  with different overrides; `served_by_model` carries the name for
  eyeballing), total ms, and an `attempts` list — one entry per member
  tried (`member`, `model`, ms, error) — so fallbacks and timeouts are
  attributable to a specific member config, joinable against the run row.
- `scope`: the chosen interpretation, candidate meanings, exclusions.
- `search`: provider id, query, ms, result count or error — per-API
  flakiness is visible directly.
- `fetch`: url, ms, ok, extracted chars. `subtask`: id, title, failed.
- `summary` (last): wall ms plus aggregates — per-member (keyed by member
  uuid, model name in the row) attempts/served/errors/total_ms/last_error,
  per-label call counts,
  per-provider search stats, fetch and subtask totals. Written in a
  `finally`, so an aborted run still ends with `"completed": false`.

Events flush line-by-line, so a crashed run keeps everything up to the
crash. Model attribution lives in `ModelCaller` (the only place fallback
attempts are visible); search/fetch events come from wrappers
(`TelemetrySearchProvider`, `telemetry_fetcher`) so the researcher stage is
untouched.

## Dependencies

Research-only packages (`trafilatura`, `ddgs` + transitives) are pinned in
`research/requirements.txt`, separate from the main `requirements.txt`, and
installed into the app venv: `venv/bin/pip install -r
research/requirements.txt`. They are imported lazily inside `research/`
only, so the main app never loads them; deleting the package and its pin
file removes the feature cleanly.
