# Naming the read: `memory_query`, `search`, and what search should become

**Date:** 2026-08-12
**Status:** Proposal. Nothing was changed.

## The premise

`memory_query` may be too unfamiliar a term for a model to reach for. "Query"
reads like SQL; the models we run have seen a hundred thousand tools called
`search`. The fear is the ordinary one: a model that does not recognize the
action as *the thing you use when you do not know* answers from its weights
instead, and a fluent invention is worse than an admission.

The sketch that goes with the rename — search returns local hits **and** three
links that widen the same query, and the ReAct loop can invoke one to reach the
web — is a separate idea, and the better of the two. It is treated on its own
below, because it survives whatever we decide about the name.

## What the model actually sees today

Worth pinning down before changing anything, because it moves where the problem
is.

`action` is a field of a grammar-constrained structured output
(`AssistantStepDecision`, `agents/assistant.py:100`), and the enum comment is
explicit that "the model can only ever name an action in this enum". The model
never types the string. It picks a member from a list rendered as
`- {value}: {description}` by `_action_catalog` (`agents/assistant.py:3750`).

So the failure this is aimed at is **not** "cannot name the tool". It is
"does not reach for it". The name is a prior on that reach, but it is one of
three signals in that catalog line, and the other two are currently weaker than
they could be:

- **The description opens badly.** `recall stored facts AND answer general
  questions (project status, git status, capabilities, model info) from the
  knowledge base` (`agents/assistant.py:2638`). "Recall" is as unfamiliar a verb
  as "query", and it leads. Whatever the action ends up called, opening that
  sentence with *search* is the cheaper half of this change and can be done
  today, independently.
- **The system prompt is already fighting for it by hand.** Two of the longest
  paragraphs in `ASSISTANT_SYSTEM_PROMPT` exist to push the model into this
  action — the read-routing rules at `agents/assistant.py:737`, and the
  "earlier messages are context, not a source of facts" passage that follows.
  Prose at that length is what you write when the affordance is not carrying
  itself. That is evidence for the premise, not against it.

## Why not a bare `search`

Three reasons, in order of weight.

**1. It competes with the other reads.** `kanban_query`, `find_uuid`, and
`workspace_read_command` are all searches in plain English. The prompt already
has to police the boundary by hand — *"Do not use `memory_query` to inspect
kanban or files"* (`agents/assistant.py:739`). Naming one action the generic
English word for looking something up puts the name in a fight with the
instruction, and names win fights with instructions. The current unfamiliar name
is at least inert; a bare `search` actively recruits the wrong traffic.

**2. It leaves its family.** The enum keeps families contiguous as
`<family>_<verb>` so they group in the rendered catalog
(`agents/assistant.py:56`), and `memory_query` sits with `memory_remember`,
`memory_activate`, `memory_forget`. A bare `search` floats free of the four
actions that operate on the thing it searches — including `memory_forget`, whose
own description tells the model where its `memory_uuid` argument comes from
(`agents/assistant.py:2673`).

**3. The widening design wants a family.** `search_memory` → widen →
`search_web` is a pair a model reads as siblings, with the prefix carrying "same
verb, different corpus". With a bare `search` alongside `search_web`, the first
reads as the general case and the second as a specialization of it — which is
the opposite of the routing intended, and invites a cold jump to the web for a
question about the operator's own life.

## Recommendation

**Rename the value to `search_memory`; keep the Python member `MEMORY_QUERY`.**

`find_uuid` is already verb-first, so verb-first inside a family is not a new
shape. This keeps the catalog grouping, leads with the familiar verb, and leaves
room for `search_web` to arrive as a sibling rather than a competitor.

**And rewrite the description to lead with the verb**, something in the shape of
*"search the knowledge base for stored facts and general questions (project/git
status, capabilities, model info) — not kanban or files"*. Two of the three
signals in that line are free to fix.

Keeping the member name means every call site stays `AssistantActionName.
MEMORY_QUERY` and the diff is confined to the value, the description, and the
prose that names the action to the model.

## The widening links are the stronger idea

The rename is a nudge on a prior. The widening design is a structural
guarantee, and it is worth building whatever happens to the name.

Today "search local memory before you reach outward" would be a rule in the
prompt, and rules in the prompt are obeyed at some rate below one. If the web
search is reachable **only** through a link handed back by the memory search,
the model cannot reach the web without having already searched memory. The
ordering stops being discipline and becomes a property of the observation. That
is the same move `find_uuid` makes for uuids: not "please do not guess", but
"here is the thing that makes guessing unnecessary".

Three design notes if it gets built:

- **Carry the query in the link, pre-filled.** The widen step should be the
  model spending a step on *retrieval*, not on re-deriving a query string it
  already wrote once. A link that arrives as
  `{"action": "search_web", "args": {"query": "solar eclipse"}}` costs the model
  nothing to take.
- **Three is the right cap, and it should be a cap.** A list of widen options
  long enough to browse is a step spent choosing between searches instead of
  doing one.
- **The empty result is the case that matters.** A search that returns nothing
  is exactly the moment the model is most likely to invent, and it is also the
  moment the widen links are most valuable. The empty observation should not
  read as "there is nothing" — it should read as "nothing local; here is where
  else to look".

## What the rename costs

The value is not only prompt text. It is written into the database and read back
by name.

- **`assistant.disabled_capabilities`** (`db/settings.py:147`) is an
  operator-set JSON list of capability **name strings**, resolved by string
  match in `_disabled_capability_names` (`agents/assistant.py:2935`). If
  `memory_query` is in that list anywhere, renaming the value silently
  re-enables the capability. Check the setting before the rename; there is no UI
  that would show it.
- **Historical `assistant_step.action` rows keep the old value**, and the trace
  timeline resolves a step's description through a map keyed by
  capability value (`webapp/assistant_views.py:27`). Every past
  `memory_query` step would lose its description. There is already a companion
  registry beside it for actions that are not capabilities
  (`webapp/assistant_views.py:33`) — a legacy entry there is the natural home
  for the old name, and costs three lines.
- **`RECALL_VERDICT_SOURCE = "memory_query.filter"`**
  (`agents/assistant.py:1207`) is written into `retrieval_event.source`. Renaming
  it splits the telemetry series; leaving it makes the source name outlive the
  action it names. Either is defensible — but pick deliberately, because
  relevance reporting reads that column and a silent split looks like a drop in
  volume.
- **The undo log stores the action string too** (`db/models.py:1272`), on the
  same terms.
- **Prose that names the action to the model** — the read-routing rules
  (`:739`), the reuse guidance (`:865`, `:895`, `:3430`), the observation footer
  that tells the model how to read a shortened fact in full (`:1430`), and
  `memory_forget`'s description (`:2673`). These are the sites where a mechanical
  rename is *not* enough, because several of them read better once the verb is
  "search".

What is **not** at risk: the in-run value comparisons. The scratchpad's
`AssistantTurnStep.action` is built from live decisions inside the run
(`agents/assistant.py:3314`), never rehydrated from stored rows, so the
trusted-fence check in `_set_observation_content` (`agents/assistant.py:5659`)
moves with the enum and cannot straddle two names.

## Measuring whether it worked

There is no harness for this today, and it is worth saying so plainly: the eval
runner is deterministic and does not run live assistant loops
(`evals/runner.py:1`), so nothing currently answers "did the run reach for the
search action".

The rows to answer it from already exist, though. `assistant_step` carries one
row per action per run with an index on `(action, phase)`
(`db/models.py:1151`), and `retrieval_event` carries what the recall filter saw.
The measurable quantity is **the share of runs that performed at least one
memory read before replying** — countable per week, before and after, without
building anything. If that share does not move, the name was not the binding
constraint and the description rewrite and the widening links are where the
remaining value is.

Worth being honest that the confound is large: model versions, prompt edits, and
the mix of questions asked all move that number. It is a signal, not a
verdict — but a rename that shows nothing over a month is a rename to
reconsider.

## Testing worth writing with it

- The catalog renders the new value and no prompt text still names the old one
  (a grep-shaped test over `_system_prompt()` output, which is where the six
  prose sites would be missed).
- A stored step whose action is the **old** value still renders with a
  description in the trace timeline.
- The disabled-capabilities setting containing the old value does not silently
  enable the action — decide the behavior first: honor the legacy name, or fail
  loudly.

## Open questions

- **Does `find_uuid` have the same problem?** It is the other action the prompt
  spends prose defending ("Never guess or fabricate a uuid",
  `agents/assistant.py:743`), which by the argument above is the tell. If the
  naming theory is right, it is right twice.
- **Is `search_web` an action, or an argument to `search_memory`?** A
  `{"scope": "web"}` argument keeps the catalog at one search and makes
  local-first the default value rather than a separate affordance. An action
  keeps the step trace and per-action cost accounting legible. This proposal
  leans to the action, but weakly, and the widening links work either way.
- **Legacy name: alias or clean break?** An alias in the enum value space costs
  a lookup and never fully retires; a clean break costs the legacy trace entry
  above and a decision about the telemetry source string. Leaning clean break,
  given the read sites are few and now enumerated.
- **Should the memory search ever answer nothing but links?** If local memory
  has no hit, the honest observation is a widen list — which makes
  `search_memory` a router in that case. That is a small design change with a
  large behavioral one behind it, and it belongs to whoever builds the widening.
