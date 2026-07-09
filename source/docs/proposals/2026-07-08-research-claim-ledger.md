# Proposal: claim ledger for the research pipeline

External review of two real runs (diesel-engine history, bicycle history)
found the pipeline's defining weakness: it verifies **provenance** — every
claim carries a `[n]` citation — but not **entailment** — that source `[n]`
actually says the claim. Reports read authoritative while containing wrong
patent dates, impossible chronology (a company acting 20 years before it
existed), and hedged source statements strengthened into absolutes
("common response to regulation" → "mandatory hardware").

## Failure analysis

Both bad runs failed through the same two structural paths, neither of
which is fixable by better sources alone:

1. **Parametric leakage upstream.** The planner and splitter write from
   model memory; a hallucinated specific ("prototype c. 1875") becomes a
   subtask description, then a search query encoding a false premise, and
   every downstream stage inherits the frame. Prompt hardening (shipped)
   reduces this but cannot eliminate it — small local models leak.
2. **Compression amplification downstream.** Each hop (source → notes →
   findings → summary) compresses, and small models systematically firm up
   hedges while compressing. No single hop is very wrong; the composition
   is.

An honest target for a local pipeline is NOT "claims are true" — that
needs world knowledge we don't trust the model to have. The achievable
target: **every load-bearing claim in the report is entailed by its cited
source text, consistent with the run's other sources, and hedged when
support is thin.** Wrong sources still produce wrong reports, but the
report stops being *more confident than its sources*, which is the current
failure.

## Design

A verification stage between the researcher and the synthesizer, mirroring
the existing map-reduce shape (many small focused calls — the only shape
that works on local models):

```
findings ──▶ extract claims ──▶ entail each claim ──▶ contradiction pass ──▶ rewrite
             (structured,        against the RAW        (per entity/date      findings from
              per section)       source extract         cluster, one          verdicts; drops
                                 it cites)              structured call)      feed Open questions
```

**1. Claim extraction** (structured, one call per findings section).
Pull the checkable claims — dates, numbers, names, firsts, causal
assertions — not every sentence:

```json
{"claims": [{"text": "DRP 67207 was granted in 1892.",
             "type": "date", "source_ids": [3]}]}
```

**2. Entailment check** (structured, one call per claim — the map step).
The user message carries the claim plus the cited sources' **raw
extracts**, not the notes: notes are themselves a compression hop, and
checking a compression against a compression lets amplified errors
through. Requires `SourceRegistry` to retain each source's extracted text
(already capped at 8 kB/source; ~20 sources/run is trivial memory).

```json
{"verdict": "contradicted",           // supported | unsupported | contradicted
 "evidence": "filed 1892-02-27, granted 1893-02-23",
 "corrected_claim": "DRP 67207 was filed in 1892 and granted in 1893."}
```

**3. Contradiction pass** (structured, one call per cluster). Claims
grouped by shared entity/number/date; the model is asked only "are these
mutually consistent?" This is what catches chronology violations
in-run — "founded 1895" and "prototype 1875" about the same entity — using
only the run's own sources, no world knowledge assumed.

**4. Rewrite** (plain, one call per findings section). Regenerate the
section from the verdicts: supported claims kept, corrected claims
substituted, contradicted claims dropped, unsupported claims either
dropped or explicitly hedged ("one low-quality source suggests…").
Dropped/contradicted claims are listed under Open questions — visible
removal, not silent deletion.

**Ledger file.** Every claim + verdict + evidence appends to
`report.claims.jsonl` next to the report and events files, same
incremental-write discipline as telemetry. The prose is then the *view*;
the ledger is the *audit trail* the reviewer asked for.

**Telemetry.** New `claim` events and summary aggregates (claims checked,
% supported/corrected/dropped, per-subtask). This makes fact discipline a
KPI you can compare across model groups exactly like latency is today.

## Triage of the reviewer's suggested gates

| gate | verdict |
|------|---------|
| claim ledger + entailment | **adopt** — the core of this proposal |
| contradiction / chronology pass | **adopt** — in-run consistency needs no world knowledge |
| overclaim detector | **adopted cheaply already** (findings prompt forbids unsupported absolutes); the entailment check catches what the prompt misses |
| source-class weighting | **adopt lite**: classify each source once (official / reference / encyclopedia / blog / marketing — one small structured call at note time), annotate References with the class, and let the rewrite hedge claims backed only by low classes. Full weighted scoring: not yet |
| date sanity (patent-number ranges etc.) | **skip as code** — over-fit to one report; the entailment check subsumes it (a source saying "granted 1893" refutes "granted 1892" regardless of domain) |
| generate prose only from verified claims | **partial** — rewrite-after-verdicts achieves the effect without abandoning the working findings stage |

## Cost

Per run with defaults (5 subtasks, ~10-15 checkable claims each):
~60-90 extra small LLM calls — roughly doubling run time on one GPU.
Config: `verify: bool = True` (`--no-verify` to opt out), because fact
discipline is the point of the tool; a draft-quality fast mode remains one
flag away. Every extra call is visible in the events file, so the
cost/quality trade is measurable per model group.

## Rollout

1. `SourceRegistry` retains raw extracts; source-class classification at
   note time; References annotated with class.
2. Claim extraction + entailment + ledger file (report unchanged — ledger
   is observe-only, so its precision can be assessed before it gains
   authority over the prose).
3. Contradiction pass + rewrite-from-verdicts + Open-questions feed; flip
   `verify` default on.
4. Benchmark: a small fixed query set with known-answer claims (the two
   reviewed reports are seed material), scored from the ledger — the
   regression harness for trying cheaper/faster model groups.

Already shipped ahead of this proposal: prompt hardening against
parametric leakage (planner/splitter/query-gen may not assert memory as
fact) and against hedge-stripping (notes preserve exact wording; findings
and summary forbid unsupported absolutes and fabricated specifics).

## Addendum: relevance discipline (second review round)

A third reviewed run (SVGA-port history) shifted the dominant failure from
"claim is false" to "claim is true but answers the wrong question": the
query term named four distinct things (display standard, DE-15 connector,
VMware's virtual GPU, embedded modules with SVGA-class resolution) and the
report blended them; component datasheets surfaced by keyword match were
treated as historical milestones.

Shipped in response (ahead of the ledger):

- **Scope stage** (`scope.py`): one structured call before planning picks
  an explicit interpretation and exclusions; the scope block travels in
  every downstream user message, the report opens with a Scope section,
  and the events file records a `scope` row.
- **Relevance prompt hardening**: selection and notes now define relevance
  as "informs the subtask as scoped" and explicitly treat datasheets,
  product listings, and same-name-different-thing pages as keyword noise.

Remaining for the ledger design (extends the source-class-lite gate): the
per-source classification gains a second axis — **relevance class** (core /
supporting / background / keyword-noise / wrong-scope) judged against the
run's chosen scope, recorded per source in the ledger; the rewrite stage
excludes keyword-noise and wrong-scope material from findings sections and
demotes it to a side note at most. Verdict vocabulary gains
`true_but_low_relevance` alongside supported/unsupported/contradicted.

## Addendum 2: evidence handling (third review round)

A fourth reviewed run (a Copenhagen folkeskole's history) validated the
scope stage and exposed the evidence layer: a founding date present in a
fetched page was lost because notes are subtask-scoped (fetched under
"architecture", discarded there, invisible to the failed "founding"
subtask); a guiding question leaked into prose as a finding; a text-rich
PDF was declared unreadable; a "falling enrollment" source became "growth"
in the report.

Shipped in response (ahead of the ledger):

- **Corpus recovery**: the registry keeps raw extracts; failed subtasks
  re-select and re-extract notes from the run's own corpus before staying
  failed.
- **Question sweeper**: deterministic post-pass moves interrogative lines
  out of findings/summary into Open questions.
- **Notes retry**: an empty/near-empty notes reply from a 4000+-char
  extract contradicts the fetch metadata and is retried once.
- **Language chaining**: plan in the query's language, subtasks in the
  plan's, findings in the subtask's.

Still the ledger's job — and now demonstrated in the wild, not
hypothetical: the trend inversion ("Faldende elevtal" → "growth") is
exactly the entailment check's target, and the generic education-finance
leakage is the relevance gate's. Raw extracts are now stored, which was
rollout step 1; the ledger can start at step 2.

## Status: implemented

A fifth reviewed run (Tycho Brahe) showed the remaining failures — invented
uncertainty ("exact dates unknown" with the dates in hand), tabloid leakage
("married Queen Mary of Denmark"), an internal contradiction (telescopic
observation in 1572 in a report that elsewhere says pre-telescopic) — were
all this proposal's territory, so it was built: `research/verifier.py`
implements tier classification, claim extraction, entailment against raw
extracts, the consistency pass, verdict-driven rewrites, and open-question
review, with the ledger at `report.claims.jsonl` (`--claims`) and
`verify: bool = True` / `--no-verify` as designed. The open-question review
(from the fifth review round) extends the original design: questions a
verified claim answers, or that manufacture doubt, are removed or narrowed.
Remaining from the rollout: step 4, the known-answer benchmark query set.

## Addendum 3: the framing layer (sixth review round)

A sixth reviewed run (the 2025 film "Obsession") showed the ledger doing
real work in the body — 14 of 34 claims dropped, including the wrong
release year — while the FRAMING layer stayed unverified: the scope stage
had fabricated a nonexistent 2017 film from parametric memory (overriding
the query's explicit "2025"), and the Scope header, summary, and open
questions kept asserting it after the body verifier dropped the same
claim. Also observed: the consistency pass flagging apparent narrative
tensions as contradictions, rewrite prose leaking "Claim 2 says…", and an
actor's process quote abstracted into a story-content claim.

Shipped on the `research-quality` branch:

- **Scope prompt**: the query's explicit attributes (year, place, version,
  person) are binding; unknown things are scoped "as the query describes
  it, to be established from sources" instead of substituted from memory.
- **Scope verification** (`verify_scope`): the chosen scope statement is
  entailment-checked against the largest fetched extracts and corrected
  when contradicted; `scope_check` row in the ledger.
- **Summary verification** (`verify_text`): the executive summary goes
  through the same extract/entail/rewrite gate as findings sections
  (origin "summary" in the ledger).
- **Consistency calibration**: apparent tensions (different moments,
  phases, framings) are explicitly not contradictions; only pairs that
  cannot both be true under any reasonable reading are flagged.
- **No verifier machinery in prose**: the rewrite prompt forbids
  mentioning claims, claim numbers, verdicts, or verification.
- **Abstraction-leap guard**: entailment treats process/circumstance
  statements as not supporting broader abstractions about the subject.
- **Source-quality caveat**: when at least half the classified sources are
  blog/marketing/tabloid, the report carries a deterministic note that it
  synthesizes commentary, not literature.

## Addendum 4: modes, resolution, and machinery hygiene (seventh round)

The seventh reviewed run (Obsession again, on the framing-layer fixes)
confirmed the scope correction works and exposed the next layer: the
corrected scope never reached the open-question review (ordering bug — it
ran last); actor names present in source [1] were declared unavailable;
a critic's rogue-LLM reading was stated as plot fact; a citation decorated
a sentence its source doesn't contain; and `[HEDGE (weak support): ...]`
lines leaked into prose despite the prompt.

Shipped on `research-quality`:

- **Ordering**: scope verification runs right after the body gate; the
  open-question review consumes the corrected scope.
- **Claim modes** (fact / interpretation / commentary): a supported
  interpretation gets action `attribute` — it may appear only as "one
  commentary reads X as ...", never as fact.
- **Open-question resolution**: surviving questions are checked against
  the run's own corpus; answerable ones become "Resolved: ..." bullets
  (`open_question_resolution` ledger rows).
- **Deterministic leak stripper**: echoed action lines and inline
  [HEDGE/KEEP/...] markers are removed in code after every rewrite —
  asking nicely demonstrably wasn't enough.
- **Citation routing**: entailment explicitly rules a claim unsupported
  when the cited source doesn't contain it, even if another source does.
- **Metaphor calibration**: literal statements and interpretive readings
  of the same thing are not contradictions.
