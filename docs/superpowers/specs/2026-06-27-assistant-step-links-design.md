# Direct links to an assistant step — design

## Goal

Let an assistant **step** be addressed directly:

1. On `/assistant`, each step in the timeline is anchorable by its uuid, with a
   small `#` permalink, so a link like `/assistant?id=<run_uuid>#step-<step_uuid>`
   opens the run and scrolls to (and highlights) that step.
2. The flask-admin `AssistantStep` list view links each row to that trace
   location (the step's `uuid` cell becomes the link).

## Part A — anchorable steps on `/assistant`

`webapp/assistant_views.py` renders the timeline as
`{% for step, intents in timeline %}<div class="step phase-{{ step.phase }}">…`,
with a header `<div class="hd"><span class="ix" …>Step N of M</span> …</div>`.
Each `step` has a stable `step.uuid`.

Changes (all in `ASSISTANT_TEMPLATE`):

- **Anchor id**: add `id="step-{{ step.uuid }}"` to the `.step` div.
- **Permalink**: in the step header, a `#` link:
  `<a class="step-anchor" href="#step-{{ step.uuid }}" title="Link to this step">#</a>`.
  Clicking it sets the address-bar hash to `#step-<uuid>` (copy/share from there).
- **Target highlight** (pure CSS): `.as-main .step:target` gets a soft highlight
  (left accent border + tint) so the linked step stands out on arrival.
- **Scroll-on-load JS**: `.as-main` is the scroll container (the page body is
  `overflow:hidden`), so a `#fragment` doesn't reliably scroll a nested scroller
  on initial load. Add a small script at the end of the existing `<script>`:
  on `DOMContentLoaded`, if `location.hash` matches `#step-…` and the element
  exists, call `el.scrollIntoView()`. (Native in-page clicks already work; this
  only covers the initial deep-link load.)

A shareable URL is `/assistant?id=<run_uuid>#step-<step_uuid>`. The run is
selected server-side by `?id=`; the step is found by its DOM id in that run's
rendered timeline. If the step uuid does not belong to the `?id=` run, nothing
scrolls (the element isn't on the page) — acceptable; the admin link always
pairs the correct `run_uuid` with the step.

## Part B — flask-admin AssistantStep → trace

`webapp/core.py` currently registers `ModelView(AssistantStep, db,
category="Assistant")` (a plain view). Replace it with a subclass that formats
the `uuid` column as a link, mirroring the existing `column_formatters` +
`Markup` pattern (`ModelConfigOverrideView`/`_format_model_config_ref`):

```python
def _format_step_trace_link(view, context, model, name):
    href = f"/assistant?id={escape(model.run_uuid)}#step-{escape(model.uuid)}"
    return Markup(f'<a href="{href}"><code>{escape(model.uuid)}</code> ↗</a>')


class AssistantStepView(ModelView):
    # Newest first; the uuid links to the run's trace, scrolled to this step.
    column_default_sort = ("id", True)
    column_formatters = {"uuid": _format_step_trace_link}


admin.add_view(AssistantStepView(AssistantStep, db, category="Assistant"))
```

The link is a plain `/assistant?id=…#step-…` string (admin templates render
raw HTML via `Markup`); no `url_for` needed, and it matches the anchor id from
Part A exactly.

## Files touched

- `webapp/assistant_views.py` — template: step `id`, `.step-anchor` permalink,
  `.step:target` CSS, scroll-on-load JS.
- `webapp/core.py` — `_format_step_trace_link` + `AssistantStepView`, swap the
  `add_view` line.
- `webapp/test_assistant_views.py` — assert step anchors + permalink render.
- `webapp/test_assistant_step_admin_link.py` (new) — unit-test the formatter.

## Testing

- **Anchors render** (`test_assistant_views.py`): for a seeded run with a step,
  `/assistant?id=<run>` body contains `id="step-<step.uuid>"` and
  `href="#step-<step.uuid>"`.
- **Admin formatter** (new test): call `_format_step_trace_link(None, None,
  step, "uuid")` with a fake/real step object exposing `run_uuid` and `uuid`;
  assert the returned Markup contains `/assistant?id=<run_uuid>#step-<uuid>`.
- (Optional, if cheap) `GET /admin/assistantstep/` returns 200 and contains a
  `#step-` link — only if the admin list renders without extra fixtures.

## Out of scope (YAGNI)

- No dedicated server route per step; the fragment + existing `?id=` suffices.
- No copy-to-clipboard button (address bar / right-click copy-link is enough).
- No change to step contents or ordering.
- No cross-run resolution on `/assistant` (looking up a step's run from a bare
  step uuid) — the admin link always carries the right `run_uuid`.
