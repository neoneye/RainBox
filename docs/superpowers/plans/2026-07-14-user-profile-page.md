# User Profile Page (`/profile`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `source/docs/proposals/2026-07-14-user-profile-page.md` — a `/profile` left-panel-tree page where each leaf is an editable person profile (registry-driven form over a JSONB `data` blob, autosaved), plus 20 shipped read-only locale templates.

**Architecture:** Rule-for-rule port of the `/prompt` page (tree-with-editor-pane variant) with the editor pane replaced by a registry-driven form. Two new tables (`profile_folder`, `profile`) mirror `PromptFolder`/`Prompt` minus version lineage, plus `data JSONB`. A Python field registry (`profile_fields.py`) is the single source of truth for validation and form rendering. The 20 built-in templates are **not DB rows** — they ship in `data/profile_templates.json` and are merged virtually into the tree GET.

**Tech Stack:** Flask + SQLAlchemy (Postgres JSONB), vanilla JS (no framework), Flask-Admin, pytest against `rainbox_claude` (conftest-forced).

**Reference implementations (already in repo — the port sources):**
- `webapp/prompt_views.py`, `webapp/prompt_api.py`, `db/prompt.py`, `static/prompt.js`
- `db/test_prompt_tree.py`, `webapp/test_prompt_api.py`, `webapp/test_prompt_views.py`
- `docs/ui-left-panel-tree.md` (tree pattern, §8 gotchas), `docs/ui-modal-rename.md`

## Global Constraints

- Work on branch `profile-page` (large feature → branch, per operator preference).
- No Python package/module named `profile` at top level (stdlib shadow); registry is `source/profile_fields.py`, db module is `db/profile.py` (inside the `db` package — safe).
- All tests run against `rainbox_claude` automatically (conftest). Never point ad-hoc scripts at production.
- No real PII anywhere; template people are deceased scientists, every other detail fictional and modern.
- Template `full_name`s/`native_name`s are the encoding test fixture — copy them **exactly** (Ø, Å, ß, ā, ḍ, U+2019 in "Ne’eman", CJK/Hangul/Telugu/Hebrew native names).
- Docs describe current state only — no "renamed from", "PR N" notes.
- The `PROFILE_TEMPLATE` Python string is a **non-raw** string: no `\n`-style escapes inside any inline JS (there is no inline JS in the template; keep it that way).
- Kebab dots via `box-shadow`, never a unicode glyph.
- New tables are additive; `db.create_all()` in `init_db` creates them — no migration code.
- Commit after each task: `git add <files> && git commit` (messages given per task).

## Fixed UUIDs (hardcoded in `data/profile_templates.json`)

Templates folder: `b3ad81a4-0e35-5237-9eb4-38c7e6321d03`

| Label | uuid |
|---|---|
| US | `430d708a-b344-57f0-b5da-a547665c534b` |
| Canada | `03052cc6-8166-52b8-bc23-d0709a8a8dc5` |
| Mexico | `145b1bb1-ee56-536a-a901-294eaf60266d` |
| Brazil | `a8578e88-eece-56df-af03-5f43b18383bb` |
| UK | `dc120eb1-ff84-51c0-ab99-80a55656fdec` |
| France | `3ef8c040-9cd7-5fdb-a051-bf4e7b5ff366` |
| Germany | `c8d7b8d3-6902-5adb-86bf-52ff5331750b` |
| Netherlands | `367ed552-9f8b-596c-8be0-a28f7a98fc93` |
| Spain | `60ebfc9f-7ab4-5f9a-981b-72e934b14af7` |
| Italy | `e1061ae1-947c-5543-ac83-75f8ed115db4` |
| Denmark | `735a5a25-c127-56cf-bbb8-fb887470761a` |
| Sweden | `4f82f338-ae59-54fc-8a08-40e93edaa3d1` |
| Poland | `14b55373-12f1-545a-a1dd-d489af095648` |
| Israel | `d7c672e8-8948-5555-a46d-0bebb3695d53` |
| India | `6c56705f-ac7d-57e6-a8d8-362d21ebbda1` |
| China | `a53912ab-b42e-5e7b-902e-13ea6d9b8fa6` |
| Japan | `ccd0961c-892a-56fb-8916-230c82bd9bc3` |
| South Korea | `88083712-ee5e-59eb-958c-d0f44ff27bce` |
| Singapore | `24adda3d-f1b8-53da-8de9-4e4a69f7f35f` |
| Australia | `92d4c457-2a20-520f-8d56-491a590ddf0a` |

---

### Task 1: Branch, field registry, models

**Files:**
- Create: `source/profile_fields.py`
- Modify: `source/db/models.py` (append after `Prompt` class, ~line 1411)
- Test: `source/db/test_profile_tree.py` (new)

**Interfaces (produces):**
- `profile_fields.Field` dataclass: `key, group, kind, label, hint="", choices=(), multiline=False, datalist=""`
- `profile_fields.PROFILE_FIELDS: list[Field]` (19 fields, 3 groups, registry order = form order)
- `profile_fields.FIELDS_BY_KEY: dict[str, Field]`
- `profile_fields.FIELD_GROUPS: list[str]` = `["Identity", "Locale & formats", "Contact & location"]`
- `profile_fields.SUMMARY_KEYS = ("full_name", "language", "units", "time_format", "country")`
- `db.models.ProfileFolder` (`profile_folder`: id, uuid, name, description, parent_uuid, position, timestamps)
- `db.models.Profile` (`profile`: id, uuid, name, folder_uuid, position, timestamps, `data: Mapped[dict] = mapped_column(JSONB, default=dict)`)

- [ ] **Step 1:** `git checkout -b profile-page`
- [ ] **Step 2: Write failing tests** in `db/test_profile_tree.py`:

```python
"""Tests for the person-profile tree persistence + data validation (db.profile,
profile_fields registry, the shipped built-in templates)."""
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

import db
import profile_fields
from db.models import Profile, ProfileFolder


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


def test_registry_shape():
    keys = [f.key for f in profile_fields.PROFILE_FIELDS]
    assert len(keys) == len(set(keys)) == 19
    assert keys[0] == "full_name"
    assert profile_fields.FIELD_GROUPS == [
        "Identity", "Locale & formats", "Contact & location"]
    for f in profile_fields.PROFILE_FIELDS:
        assert f.kind in {"text", "enum", "date", "email"}
        if f.kind == "enum":
            assert f.choices
        else:
            assert f.choices == ()
    for k in profile_fields.SUMMARY_KEYS:
        assert k in profile_fields.FIELDS_BY_KEY


def test_profile_models_round_trip(app_ctx):
    fu, pu = uuid4(), uuid4()
    db.db.session.add(ProfileFolder(uuid=fu, name="T-folder", parent_uuid=None, position=0))
    db.db.session.add(Profile(uuid=pu, name="T-profile", folder_uuid=fu, position=0,
                              data={"full_name": "Ada Test", "units": "metric"}))
    db.db.session.commit()
    try:
        f = db.db.session.execute(sa.select(ProfileFolder).where(ProfileFolder.uuid == fu)).scalar_one()
        p = db.db.session.execute(sa.select(Profile).where(Profile.uuid == pu)).scalar_one()
        assert f.name == "T-folder" and f.parent_uuid is None
        assert p.data == {"full_name": "Ada Test", "units": "metric"}
        assert p.folder_uuid == fu
        assert f.created_at and p.updated_at
    finally:
        db.db.session.execute(sa.delete(Profile).where(Profile.uuid == pu))
        db.db.session.execute(sa.delete(ProfileFolder).where(ProfileFolder.uuid == fu))
        db.db.session.commit()
```

- [ ] **Step 3:** Run `pytest source/db/test_profile_tree.py -v` → FAIL (no module `profile_fields`).
- [ ] **Step 4: Implement.** `source/profile_fields.py` — the registry exactly as the proposal's code block (keys, groups, kinds, labels, hints, choices, `multiline`, `datalist`), with the `Field` dataclass above and derived `FIELDS_BY_KEY` / `FIELD_GROUPS` / `SUMMARY_KEYS`. `db/models.py` — the two model classes mirroring `PromptFolder`/`Prompt` (no `parent_uuid` on `Profile`, add `data` JSONB, indexes `profile_folder_children` and `profile_in_folder`). Docstrings: `Profile.name` is the standalone tree label, never derived from `data["full_name"]`; `data["dynamic"]` is connector-owned.
- [ ] **Step 5:** Run tests → PASS. Commit: `feat: person-profile field registry + profile tables`

### Task 2: `validate_profile_data`

**Files:**
- Create: `source/db/profile.py`
- Modify: `source/db/__init__.py` (add `from db.profile import *` re-export next to the prompt one)
- Test: `source/db/test_profile_tree.py` (append)

**Interfaces (produces):**
- `db.ProfileTreeError(ValueError)`, `db.ProfileTreeConflict(Exception)`, `db.ProfileDataError(ValueError)`
- `db.validate_profile_data(data: Any) -> dict` — returns canonical sparse editable object; raises `ProfileDataError` naming the field.

Rules (proposal §Data model): dict required; `dynamic` → rejected read-only; unknown key → rejected **even when empty**; known key with `""` → dropped (canonicalized); all v1 kinds are strings; enum must be in `choices`; date must match `^\d{4}-\d{2}-\d{2}$` AND be a real calendar date (`datetime.date.fromisoformat` after the regex — the regex blocks `20260230`-style basic format, fromisoformat blocks `2026-02-30`). No IANA/BCP-47/ISO-4217 membership gatekeeping (soft validation).

- [ ] **Step 1: Failing tests** (append to `db/test_profile_tree.py`):

```python
def test_validate_data_canonical_and_errors():
    ok = db.validate_profile_data({
        "full_name": "Jacobus van 't Hoff", "units": "metric",
        "birthday": "1987-08-30", "address": "Line one\nLine two",
        "timezone": "", "email": "x@example.com"})
    assert ok == {"full_name": "Jacobus van 't Hoff", "units": "metric",
                  "birthday": "1987-08-30", "address": "Line one\nLine two",
                  "email": "x@example.com"}          # "" canonicalized away
    assert db.validate_profile_data({}) == {}        # sparse blob valid
    with pytest.raises(db.ProfileDataError, match="no_such"):
        db.validate_profile_data({"no_such": "x"})
    with pytest.raises(db.ProfileDataError, match="no_such"):
        db.validate_profile_data({"no_such": ""})    # unknown stays rejected when empty
    with pytest.raises(db.ProfileDataError, match="units"):
        db.validate_profile_data({"units": "furlongs"})
    with pytest.raises(db.ProfileDataError, match="birthday"):
        db.validate_profile_data({"birthday": "2026-02-30"})
    with pytest.raises(db.ProfileDataError, match="birthday"):
        db.validate_profile_data({"birthday": "07/14/2026"})
    with pytest.raises(db.ProfileDataError, match="dynamic"):
        db.validate_profile_data({"dynamic": {}})    # connector-owned, read-only
    with pytest.raises(db.ProfileDataError, match="full_name"):
        db.validate_profile_data({"full_name": 5})
    with pytest.raises(db.ProfileDataError):
        db.validate_profile_data(["not", "a", "dict"])
```

- [ ] **Step 2:** Run → FAIL. **Step 3: Implement** `db/profile.py` (module docstring mirrors `db/prompt.py`'s style: backs `/profile`, tree bulk pattern + per-profile data ops + shipped built-ins) with the three exception classes, `_to_uuid`, and:

```python
def validate_profile_data(data: Any) -> dict[str, Any]:
    """Validate a complete editable snapshot against the registry and return
    the canonical sparse object: known editable keys only, "" values removed
    before validation, string kinds checked strictly (enum membership, ISO
    calendar date). Soft on IANA/BCP-47/4217 membership by design. `dynamic`
    is connector-owned and rejected as read-only. Raises ProfileDataError
    naming the offending field."""
    if not isinstance(data, dict):
        raise ProfileDataError(f"'data' must be an object, got {type(data).__name__}")
    canonical: dict[str, Any] = {}
    for key, value in data.items():
        if key == "dynamic":
            raise ProfileDataError("field 'dynamic' is read-only (connector-owned)")
        field = FIELDS_BY_KEY.get(key)
        if field is None:
            raise ProfileDataError(f"unknown field: '{key}'")
        if value == "":
            continue  # canonicalize: blank means absent, the JSONB stays sparse
        if not isinstance(value, str):
            raise ProfileDataError(f"field '{key}' must be a string, got {type(value).__name__}")
        if field.kind == "enum" and value not in field.choices:
            raise ProfileDataError(f"field '{key}' must be one of {list(field.choices)}, got {value!r}")
        if field.kind == "date":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ProfileDataError(f"field '{key}' must be an ISO date (YYYY-MM-DD), got {value!r}")
            try:
                date.fromisoformat(value)
            except ValueError:
                raise ProfileDataError(f"field '{key}' is not a valid calendar date: {value!r}") from None
        canonical[key] = value
    return canonical
```

- [ ] **Step 4:** Run → PASS. Commit: `feat: profile data validator (registry-driven, canonical sparse output)`

### Task 3: Tree persistence

**Files:** Modify `source/db/profile.py`; test appends to `db/test_profile_tree.py`.

**Interfaces (produces):**
- `db.profile_data_summary(data: dict) -> dict` — `{k: data.get(k, "") for k in SUMMARY_KEYS}`
- `db.profile_tree_version() -> str` — sha256[:16] over structural fields only: folders `[uuid, name, description, parent, position]`, profiles `[uuid, name, folder, position]` (no `data`, no summary; DB rows only)
- `db.profile_load_tree() -> {"folders": [...], "profiles": [...], "version": str}` — frontend field names (`id/parentId`, `uuid/folderId`), each profile row carries `summary` (built-ins merge in Task 4)
- `db.validate_profile_tree(folders, profiles) -> None` — port of `validate_prompt_tree` minus `parentUuid`, **plus**: a profile entry carrying a `summary` key → `ProfileTreeError("... derived 'summary' must not be submitted")`
- `db.profile_save_tree(folders, profiles, *, base_version=None, expected_deletes=None)` — port of `prompt_save_tree`; never touches `data` (new rows start `{}`, existing rows keep theirs)

Port `db/prompt.py` functions renaming `prompt→profile`, `Prompt→Profile`, `PromptFolder→ProfileFolder`, `prompts→profiles`; drop everything lineage/content/clone/diff related.

- [ ] **Step 1: Failing tests** (append; fixtures `profile_tree_snapshot` + `empty_tree` are the `prompt_tree_snapshot` pattern ported to the two profile tables):

```python
@pytest.fixture
def profile_tree_snapshot(app_ctx):
    def grab(model):
        rows = db.db.session.execute(sa.select(model)).scalars().all()
        return [{c.name: getattr(r, c.name) for c in model.__table__.columns if c.name != "id"}
                for r in rows]
    fsnap, psnap = grab(ProfileFolder), grab(Profile)
    try:
        yield
    finally:
        db.db.session.execute(sa.delete(Profile))
        db.db.session.execute(sa.delete(ProfileFolder))
        for row in fsnap:
            db.db.session.add(ProfileFolder(**row))
        for row in psnap:
            db.db.session.add(Profile(**row))
        db.db.session.commit()


@pytest.fixture
def empty_tree(profile_tree_snapshot):
    db.db.session.execute(sa.delete(Profile))
    db.db.session.execute(sa.delete(ProfileFolder))
    db.db.session.commit()


def test_save_and_load_roundtrip(app_ctx, empty_tree):
    f_root, f_child, pr = str(uuid4()), str(uuid4()), str(uuid4())
    db.profile_save_tree(
        [{"id": f_root, "name": "Friends", "description": "top", "parentId": None},
         {"id": f_child, "name": "Copenhagen", "parentId": f_root}],
        [{"uuid": pr, "name": "Simon", "folderId": f_child}])
    out = db.profile_load_tree()
    user_folders = [f for f in out["folders"] if not f.get("builtin")]
    user_profiles = [p for p in out["profiles"] if not p.get("builtin")]
    assert [f["name"] for f in user_folders] == ["Friends", "Copenhagen"]
    assert user_folders[1]["parentId"] == f_root
    assert user_profiles[0]["folderId"] == f_child
    assert "data" not in user_profiles[0]           # blob stays out of the tree payload
    assert set(user_profiles[0]["summary"]) == set(profile_fields.SUMMARY_KEYS)
    assert out["version"]


def test_tree_save_preserves_data(app_ctx, empty_tree):
    pr = str(uuid4())
    db.profile_save_tree([], [{"uuid": pr, "name": "P", "folderId": None}])
    row = db.db.session.execute(sa.select(Profile).where(Profile.uuid == UUID(pr))).scalar_one()
    row.data = {"full_name": "Keep Me"}
    db.db.session.commit()
    db.profile_save_tree([], [{"uuid": pr, "name": "P renamed", "folderId": None}])
    row = db.db.session.execute(sa.select(Profile).where(Profile.uuid == UUID(pr))).scalar_one()
    assert row.name == "P renamed" and row.data == {"full_name": "Keep Me"}


def test_version_conflict(app_ctx, profile_tree_snapshot):
    with pytest.raises(db.ProfileTreeConflict):
        db.profile_save_tree([], [], base_version="stale-token-xyz")


def test_delete_tripwire(app_ctx, empty_tree):
    f = str(uuid4())
    db.profile_save_tree([{"id": f, "name": "F", "parentId": None}], [])
    with pytest.raises(db.ProfileTreeError):
        db.profile_save_tree([], [], expected_deletes=0)


def test_validate_rejects_dangling_cycle_collision_summary(app_ctx):
    with pytest.raises(db.ProfileTreeError):
        db.validate_profile_tree([], [{"uuid": str(uuid4()), "name": "P",
                                       "folderId": str(uuid4())}])
    a, b = str(uuid4()), str(uuid4())
    with pytest.raises(db.ProfileTreeError):
        db.validate_profile_tree([{"id": a, "name": "A", "parentId": b},
                                  {"id": b, "name": "B", "parentId": a}], [])
    shared = str(uuid4())
    with pytest.raises(db.ProfileTreeError):
        db.validate_profile_tree([{"id": shared, "name": "F", "parentId": None}],
                                 [{"uuid": shared, "name": "P", "folderId": None}])
    with pytest.raises(db.ProfileTreeError, match="summary"):
        db.validate_profile_tree([], [{"uuid": str(uuid4()), "name": "P",
                                       "folderId": None, "summary": {}}])
```

- [ ] **Step 2:** FAIL → **Step 3: implement** (port as specified) → **Step 4:** PASS. Commit: `feat: profile tree persistence (load/save/version/validate)`

### Task 4: Built-in templates

**Files:** Create `source/data/profile_templates.json`; modify `source/db/profile.py`; test appends.

**Interfaces (produces):**
- `db.profile_templates_folder_uuid() -> UUID`
- `db.profile_builtin_uuids() -> frozenset[UUID]` (folder + 20 profiles)
- `db.profile_builtin_get(uuid: UUID) -> dict | None` — `{"uuid", "name", "data"}`
- `db.profile_templates_entries() -> list[dict]` — the 20 entries, file order
- `profile_load_tree` now appends the virtual folder `{"id": <folder-uuid>, "name": "Templates", "description": <from file>, "parentId": None, "builtin": True}` **after** user folders, and the 20 profiles `{"uuid", "name", "folderId": <folder-uuid>, "builtin": True, "summary": ...}` **after** user profiles. Excluded from `version` by construction (they are never DB rows).
- `validate_profile_tree` additionally rejects any folder id / profile uuid in `profile_builtin_uuids()` → `ProfileTreeError("... is a read-only built-in")`.

JSON shape:

```json
{
  "folder": {"uuid": "b3ad81a4-0e35-5237-9eb4-38c7e6321d03", "name": "Templates",
             "description": "Built-in locale archetypes — duplicate one to start a profile. Ships with rainbox and updates with each release."},
  "profiles": [ {"uuid": "...", "name": "US", "data": {...}}, ... 20 entries ... ]
}
```

Entry data (all 20; uuids from the table above; file order = Americas, Europe, Middle East, Asia, Oceania — exactly the label order of the uuid table). Common shape per entry — every key below present unless marked; `handle`/`email`/`address` always absent:
`full_name`, `native_name` (only Israel, India, China, Japan, South Korea, Singapore), `preferred_name`, `gender`, `about`, `birthday`, `units`, `timezone`, `date_format`, `time_format`, `language`, `language_2` (absent for US, UK, Australia), `currency`, `country` (= label), `city`.

| Label | full_name | preferred_name | gender | birthday | about |
|---|---|---|---|---|---|
| US | Raymond Davis Jr. | Raymond | male | 1984-10-19 | detected solar neutrinos |
| Canada | Conrad Kirouac | Frère Marie-Victorin | male | 1990-04-03 | wrote the Flore laurentienne, Québec's definitive botany |
| Mexico | Ynés Mexía | Ynés | female | 1988-05-24 | discovered some 500 new plant species |
| Brazil | Maurício Rocha e Silva | Maurício | male | 1992-09-19 | discovered bradykinin, the blood-pressure peptide |
| UK | D'Arcy Wentworth Thompson | D'Arcy | male | 1986-05-02 | founded mathematical biology (On Growth and Form) |
| France | Émilie du Châtelet | Émilie | female | 1993-12-17 | showed kinetic energy scales with velocity squared |
| Germany | Karl Weierstraß | Karl | male | 1985-10-31 | made calculus rigorous (ε–δ) |
| Netherlands | Jacobus van 't Hoff | Jacobus | male | 1987-08-30 | founded stereochemistry |
| Spain | Santiago Ramón y Cajal | Santiago | male | 1989-05-01 | showed the brain is made of neurons |
| Italy | Rita Levi-Montalcini | Rita | female | 1991-04-22 | discovered nerve growth factor |
| Denmark | Øjvind Winge | Øjvind | male | 1983-05-19 | founded the genetics of yeast |
| Sweden | Anders Jonas Ångström | Anders | male | 1994-08-13 | pioneered spectroscopy |
| Poland | Zofia Kielan-Jaworowska | Zofia | female | 1982-04-25 | led the Gobi expeditions that rewrote early-mammal evolution |
| Israel | Yuval Ne’eman | Yuval | male | 1990-05-14 | ordered the particle zoo (SU(3)) |
| India | Yallāpragaḍa Subbārāvu | Subbārāvu | male | 1995-01-12 | co-created methotrexate chemotherapy |
| China | Wu Chien-Shiung | Chien-Shiung | female | 1987-05-31 | overthrew parity conservation |
| Japan | Hideki Yukawa | Hideki | male | 1989-01-23 | predicted the meson |
| South Korea | Woo Jang-choon | Jang-choon | male | 1996-04-08 | the triangle of U |
| Singapore | Wu Lien-teh | Lien-teh | male | 1986-03-10 | pioneered modern epidemic control |
| Australia | Ferdinand Jakob Heinrich von Mueller | Ferdinand | male | 1984-06-30 | documented Australia's flora |

Locale columns (units/time/date/lang/lang2/currency/city/timezone) verbatim from the proposal's table (Israel note: "Ne’eman" uses U+2019). `native_name`: Israel `יובל נאמן`, India `యల్లాప్రగడ సుబ్బారావు`, China `吳健雄`, Japan `湯川秀樹`, South Korea `우장춘`, Singapore `伍連德`.

Loader in `db/profile.py`:

```python
_TEMPLATES_PATH = Path(__file__).resolve().parent.parent / "data" / "profile_templates.json"

@lru_cache(maxsize=1)
def _templates() -> dict[str, Any]:
    """The shipped built-in templates file, parsed once per process. The file
    is part of the release — a new rainbox serves new content on next load."""
    return json.loads(_TEMPLATES_PATH.read_text(encoding="utf-8"))
```

- [ ] **Step 1: Failing tests:**

```python
def test_builtins_merged_into_tree(app_ctx, empty_tree):
    out = db.profile_load_tree()
    tf = str(db.profile_templates_folder_uuid())
    builtin_folders = [f for f in out["folders"] if f.get("builtin")]
    assert [f["id"] for f in builtin_folders] == [tf]
    builtins = [p for p in out["profiles"] if p.get("builtin")]
    assert len(builtins) == 20
    assert all(p["folderId"] == tf for p in builtins)
    assert builtins[0]["name"] == "US" and builtins[-1]["name"] == "Australia"
    assert builtins[6]["summary"]["full_name"] == "Karl Weierstraß"
    assert "data" not in builtins[0]


def test_builtins_excluded_from_version(app_ctx, empty_tree):
    # An empty DB hashes as an empty tree even though GET merges 21 virtual rows.
    assert db.profile_tree_version() == db.profile_tree_version()
    out = db.profile_load_tree()
    assert len(out["profiles"]) == 20 and len(out["folders"]) == 1
    db.profile_save_tree([], [], base_version=out["version"])  # builtin-free save is a no-op


def test_tree_put_rejects_builtin_uuids(app_ctx):
    tf = str(db.profile_templates_folder_uuid())
    bp = str(next(iter(db.profile_builtin_uuids() - {db.profile_templates_folder_uuid()})))
    with pytest.raises(db.ProfileTreeError, match="built-in"):
        db.validate_profile_tree([{"id": tf, "name": "Templates", "parentId": None}], [])
    with pytest.raises(db.ProfileTreeError, match="built-in"):
        db.validate_profile_tree([], [{"uuid": bp, "name": "X", "folderId": None}])


def test_all_templates_validate(app_ctx):
    entries = db.profile_templates_entries()
    assert len(entries) == 20
    for e in entries:
        canonical = db.validate_profile_data(e["data"])
        assert canonical == e["data"]        # shipped data is already canonical (no "" values)
        assert e["data"]["country"] == e["name"]
        assert "handle" not in e["data"] and "email" not in e["data"]
```

- [ ] **Step 2:** FAIL → **Step 3:** write the JSON + loader + merge + validator guard → **Step 4:** PASS. Commit: `feat: shipped built-in profile templates (virtual, read-only)`

### Task 5: Per-profile data ops

**Files:** Modify `source/db/profile.py`; test appends.

**Interfaces (produces):**
- `db.profile_get(uuid) -> dict | None` — `{"uuid","name","data","builtin"}` (+timestamps for user rows); built-ins served from the file with `builtin: True`
- `db.profile_update_data(uuid, data) -> dict | None` — validates (raises `ProfileDataError`), replaces editable keys with the canonical snapshot, **preserves the pre-update `dynamic` subtree in the same transaction**, returns the new summary; `None` for unknown uuid. Built-in rejection is the API layer's job.
- `db.profile_duplicate(uuid) -> dict | None` — user source: new row `"<name> copy"`, same folder, position source+1 (shift later siblings, like `prompt_clone`); built-in source: new row named after the template at the **end of the user-owned top level** (`max(position)+1` among `folder_uuid IS NULL`). Deep-copies the complete stored blob **including `dynamic`**. Returns tree-row shape + `summary`.

- [ ] **Step 1: Failing tests:**

```python
def test_update_data_merges_and_deletes(app_ctx, empty_tree):
    pr = str(uuid4())
    db.profile_save_tree([], [{"uuid": pr, "name": "P", "folderId": None}])
    v = db.profile_tree_version()
    dynamic = {"location": {"value": "Copenhagen", "seen_at": "2026-07-14T10:00:00+00:00"}}
    row = db.db.session.execute(sa.select(Profile).where(Profile.uuid == UUID(pr))).scalar_one()
    row.data = {"full_name": "Old Name", "city": "Aarhus", "dynamic": dynamic}
    db.db.session.commit()
    summary = db.profile_update_data(UUID(pr), {"full_name": "New Name", "units": "metric"})
    assert summary["full_name"] == "New Name"
    stored = db.profile_get(UUID(pr))["data"]
    assert stored["dynamic"] == dynamic            # observation survives byte-for-byte
    assert "city" not in stored                    # omitted editable key deleted, not retained
    assert stored["full_name"] == "New Name" and stored["units"] == "metric"
    assert db.profile_tree_version() == v          # data excluded from the structural version
    tree_row = [p for p in db.profile_load_tree()["profiles"] if p["uuid"] == pr][0]
    assert tree_row["summary"]["full_name"] == "New Name"   # summary rides, version stable
    assert db.profile_update_data(uuid4(), {}) is None


def test_duplicate_user_owned(app_ctx, empty_tree):
    f, src, other = str(uuid4()), str(uuid4()), str(uuid4())
    db.profile_save_tree([{"id": f, "name": "F", "parentId": None}],
                         [{"uuid": src, "name": "Simon", "folderId": f},
                          {"uuid": other, "name": "After", "folderId": f}])
    blob = {"full_name": "Simon S", "dynamic": {"screen": {"value": "3440x1440",
                                                           "seen_at": "2026-07-01T00:00:00+00:00"}}}
    row = db.db.session.execute(sa.select(Profile).where(Profile.uuid == UUID(src))).scalar_one()
    row.data = blob
    db.db.session.commit()
    dup = db.profile_duplicate(UUID(src))
    assert dup["name"] == "Simon copy" and dup["folderId"] == f
    got = db.profile_get(UUID(dup["uuid"]))
    assert got["data"] == blob and got["data"] is not blob   # deep copy, dynamic included
    order = [p["uuid"] for p in db.profile_load_tree()["profiles"] if not p.get("builtin")]
    assert order.index(dup["uuid"]) == order.index(src) + 1
    assert db.profile_duplicate(uuid4()) is None


def test_duplicate_builtin(app_ctx, empty_tree):
    pr = str(uuid4())
    db.profile_save_tree([], [{"uuid": pr, "name": "Existing", "folderId": None}])
    germany = next(e for e in db.profile_templates_entries() if e["name"] == "Germany")
    dup = db.profile_duplicate(UUID(germany["uuid"]))
    assert dup["name"] == "Germany" and dup["folderId"] is None
    got = db.profile_get(UUID(dup["uuid"]))
    assert got["builtin"] is False and got["data"] == germany["data"]   # real, editable row
    roots = [p for p in db.profile_load_tree()["profiles"]
             if not p.get("builtin") and p["folderId"] is None]
    assert roots[-1]["uuid"] == dup["uuid"]        # end of the user-owned top level
```

- [ ] **Step 2:** FAIL → **Step 3:** implement → **Step 4:** PASS. Commit: `feat: profile get/update/duplicate with dynamic-subtree preservation`

### Task 6: JSON API

**Files:** Create `source/webapp/profile_api.py`; modify `source/webapp/__init__.py` (import `profile_views` then `profile_api` after the prompt pair — `profile_views` arrives Task 7, so import only `profile_api` now and add `profile_views` in Task 7); Test: `source/webapp/test_profile_api.py` (new).

**Interfaces (produces):** routes exactly as the proposal §API:
- `GET/PUT /profile/api/tree` — port of `prompt_tree` (version + non-negative int `deletes` guards; 409 with fresh version on `ProfileTreeConflict`; 400 on `ProfileTreeError`)
- `GET /profile/api/profiles/<uuid>` → `{ok, uuid, name, data, builtin}`; 400 bad uuid; 404 unknown
- `PUT /profile/api/profiles/<uuid>` body `{data: {...}}` → validates; **built-in uuid → 400 `"read-only built-in"` (checked before anything else)**; `ProfileDataError` → 400 with its message; 404 unknown; 200 → `{ok: True, summary}`
- `POST /profile/api/profiles/<uuid>/duplicate` → `{ok: True, profile: <tree-row + summary>}`; 404 unknown

Full implementation is the `prompt_api.py` port with those deltas (no clone/diff routes). Module docstring mirrors `prompt_api.py`'s.

- [ ] **Step 1: Failing tests** (`webapp/test_profile_api.py`, same live-Postgres style as `test_prompt_api.py` — `_cleanup(uuids)` deleting `Profile` rows, `_seed_profile(client, name)` via the tree PUT):

```python
"""Tests for webapp/profile_api.py. Live local Postgres via conftest."""
from uuid import uuid4

import sqlalchemy as sa

import db
from db.models import Profile
from webapp.core import app


def _cleanup(profile_uuids):
    a = db.make_app()
    db.init_db(a)
    with a.app_context():
        db.db.session.execute(sa.delete(Profile).where(Profile.uuid.in_(profile_uuids)))
        db.db.session.commit()


def _seed_profile(client, name="ApiTest"):
    tree = client.get("/profile/api/tree").get_json()
    pu = str(uuid4())
    folders = [{"id": f["id"], "name": f["name"], "description": f.get("description") or "",
                "parentId": f.get("parentId")} for f in tree["folders"] if not f.get("builtin")]
    profiles = [{"uuid": p["uuid"], "name": p["name"], "folderId": p.get("folderId")}
                for p in tree["profiles"] if not p.get("builtin")]
    profiles.append({"uuid": pu, "name": name, "folderId": None})
    resp = client.put("/profile/api/tree", json={
        "folders": folders, "profiles": profiles,
        "version": tree["version"], "deletes": 0})
    assert resp.status_code == 200, resp.get_json()
    return pu


def test_tree_get_shape_includes_builtins():
    out = app.test_client().get("/profile/api/tree").get_json()
    assert isinstance(out["folders"], list) and isinstance(out["profiles"], list)
    assert out["version"]
    builtins = [p for p in out["profiles"] if p.get("builtin")]
    assert len(builtins) == 20
    assert all("summary" in p for p in out["profiles"])
    assert all("data" not in p for p in out["profiles"])


def test_tree_put_guards():
    client = app.test_client()
    assert client.put("/profile/api/tree",
                      json={"folders": [], "profiles": []}).status_code == 400
    tree = client.get("/profile/api/tree").get_json()
    resp = client.put("/profile/api/tree", json={
        "folders": [], "profiles": [], "version": "stale-token-xyz", "deletes": 0})
    assert resp.status_code == 409 and resp.get_json()["version"]
    # A payload carrying a built-in uuid is refused outright.
    bp = next(p for p in tree["profiles"] if p.get("builtin"))
    resp = client.put("/profile/api/tree", json={
        "folders": [], "profiles": [{"uuid": bp["uuid"], "name": "X", "folderId": None}],
        "version": tree["version"], "deletes": 0})
    assert resp.status_code == 400


def test_data_roundtrip_canonicalize_and_summary():
    client = app.test_client()
    pu = _seed_profile(client)
    try:
        got = client.get(f"/profile/api/profiles/{pu}").get_json()
        assert got["ok"] is True and got["data"] == {} and got["builtin"] is False
        resp = client.put(f"/profile/api/profiles/{pu}",
                          json={"data": {"full_name": "Ada T", "city": "", "units": "metric"}})
        assert resp.status_code == 200
        assert resp.get_json()["summary"]["full_name"] == "Ada T"
        got = client.get(f"/profile/api/profiles/{pu}").get_json()
        assert got["data"] == {"full_name": "Ada T", "units": "metric"}  # "" canonicalized away
    finally:
        _cleanup([pu])


def test_data_put_rejections():
    client = app.test_client()
    pu = _seed_profile(client)
    try:
        r = client.put(f"/profile/api/profiles/{pu}", json={"data": {"units": "furlongs"}})
        assert r.status_code == 400 and "units" in r.get_json()["error"]
        r = client.put(f"/profile/api/profiles/{pu}", json={"data": {"dynamic": {}}})
        assert r.status_code == 400
        assert client.put(f"/profile/api/profiles/{pu}", json={"data": "nope"}).status_code == 400
    finally:
        _cleanup([pu])


def test_builtin_read_only_and_duplicate():
    client = app.test_client()
    tree = client.get("/profile/api/tree").get_json()
    bp = next(p for p in tree["profiles"] if p.get("builtin") and p["name"] == "Denmark")
    got = client.get(f"/profile/api/profiles/{bp['uuid']}").get_json()
    assert got["ok"] is True and got["builtin"] is True
    assert got["data"]["full_name"] == "Øjvind Winge"
    r = client.put(f"/profile/api/profiles/{bp['uuid']}", json={"data": {}})
    assert r.status_code == 400 and "built-in" in r.get_json()["error"]
    res = client.post(f"/profile/api/profiles/{bp['uuid']}/duplicate").get_json()
    try:
        assert res["ok"] is True and res["profile"]["name"] == "Denmark"
        assert res["profile"]["folderId"] is None
    finally:
        _cleanup([res["profile"]["uuid"]])


def test_duplicate_user_owned_copies_data():
    client = app.test_client()
    pu = _seed_profile(client, name="DupSrc")
    created = [pu]
    try:
        client.put(f"/profile/api/profiles/{pu}", json={"data": {"full_name": "Src Person"}})
        res = client.post(f"/profile/api/profiles/{pu}/duplicate").get_json()
        assert res["ok"] is True
        created.append(res["profile"]["uuid"])
        assert res["profile"]["name"] == "DupSrc copy"
        got = client.get(f"/profile/api/profiles/{res['profile']['uuid']}").get_json()
        assert got["data"] == {"full_name": "Src Person"}
    finally:
        _cleanup(created)


def test_bad_and_unknown_uuids():
    client = app.test_client()
    assert client.get("/profile/api/profiles/not-a-uuid").status_code == 400
    assert client.get(f"/profile/api/profiles/{uuid4()}").status_code == 404
    assert client.put(f"/profile/api/profiles/{uuid4()}", json={"data": {}}).status_code == 404
    assert client.post(f"/profile/api/profiles/{uuid4()}/duplicate").status_code == 404
```

- [ ] **Step 2:** FAIL → **Step 3:** implement + register import → **Step 4:** PASS. Commit: `feat: /profile JSON API (tree + data + duplicate)`

### Task 7: Views, nav, admin

**Files:** Create `source/webapp/profile_views.py`; modify `source/webapp/core.py` (nav link + admin views + model imports), `source/webapp/__init__.py` (import `profile_views` before `profile_api`); Test: `source/webapp/test_profile_views.py` (new).

**Interfaces (produces):**
- Route `/profile` → endpoint `profile_page`, rendering `PROFILE_TEMPLATE` with `profile_js_v` (mtime cache-buster, same `_prompt_js_version` pattern) and `form_fields=Markup(_form_fields_html())`.
- `_form_fields_html()` — generated from `PROFILE_FIELDS`: one `<fieldset class="profile-fieldset">` per group with `<legend>`, per field `<label for="pf-<key>" title="<hint>">` + input (`enum` → `<select data-key>` with leading blank `<option>` (a form affordance, not a registry choice); `multiline` → `<textarea rows="3">`; `date`/`email` → typed `<input>`; text with `datalist` → `list="profile-dl-<datalist>"`). After the "Locale & formats" fields, inside its fieldset: `<div id="profile-preview" class="muted"></div>`.
- Page markup (port of `PROMPT_TEMPLATE`, `prompt-`→`profile-` throughout): nav include + `.pp-nav{margin-bottom:0}`, split layout, tree chrome in `/cron` order (`All profiles` → hr → `+ Folder` / `+ Profile` buttons → hr → tree ul → root-drop strip), right pane: `#profile-node-rename`, `#profile-folder-desc`, folder table (`<thead>` **Name / Person / Language / Units / Time / Country** + trailing blank th), `#profile-form` (hidden) containing `#profile-save-status` + `#profile-builtin-hint` ("Built-in template — Duplicate to make an editable copy", hidden) + `{{ form_fields }}` + `<fieldset id="profile-dynamic" hidden><legend>Last seen</legend><div id="profile-dynamic-rows"></div></fieldset>`, 4 datalists (`profile-dl-tz/lang/currency/country`, empty — JS fills), the five modals (folder, new-profile, rename, desc, delete w/ type-to-confirm), toast, `<script src="/static/profile.js?v=...">`. **No CodeMirror, no editor/diff markup, no inline `<script>` blocks.**
- CSS: port prompt CSS 1:1 (rename classes), drop editor/diff/toolbar rules, add: form field styling, `.profile-fieldset` box, `#profile-form{max-width:560px}`, disabled-input styling, `.profile-builtin-tag{font-size:0.7rem;color:#6b7280;border:1px solid #d1d5db;border-radius:4px;padding:0 4px;margin-left:6px}`, `.profile-kebab-none{visibility:hidden !important}`.
- Nav: `<a href="{{ url_for('profile_page') }}" class="{{ 'pp-active' if request.endpoint == 'profile_page' }}">Profile</a>` inserted immediately before the Settings link.
- Admin (`core.py`): import `Profile, ProfileFolder`; `_profile_open_link` / `_profile_folder_label` (same pattern as the prompt ones, linking `/profile?id=`); `ProfileFolderView` (columns as PromptFolderView) and `ProfileView` (columns `profile_link, position, uuid, name, folder_uuid, data, created_at, updated_at`); `admin.add_view(..., category="Profile")` × 2, placed after the Prompt block.

- [ ] **Step 1: Failing tests** (`webapp/test_profile_views.py`):

```python
"""Tests for webapp/profile_views.py (+ the profile.js reference).

Marker-string tests prove presence, not behaviour — the real tree/form
behaviours are verified in a browser per docs/ui-left-panel-tree.md §8."""
from pathlib import Path

from webapp.core import app
import webapp.profile_views as profile_views


def _page() -> str:
    return app.test_client().get("/profile").get_data(as_text=True)


def test_profile_page_renders_with_nav():
    body = _page()
    assert 'class="profile-split"' in body
    assert "pp-nav" in body
    assert "/static/profile.js?v=" in body    # mtime cache-busted external JS


def test_nav_has_profile_link():
    assert ">Profile<" in _page()


def test_form_fieldsets_from_registry():
    body = _page()
    for legend in ("Identity", "Locale &amp; formats", "Contact &amp; location"):
        assert f"<legend>{legend}</legend>" in body
    for key in ("full_name", "native_name", "preferred_name", "handle", "gender",
                "about", "birthday", "units", "timezone", "date_format",
                "time_format", "language", "language_2", "currency", "currency_2",
                "country", "city", "address", "email"):
        assert f'data-key="{key}"' in body, f"missing field {key}"
    for dl in ("profile-dl-tz", "profile-dl-lang", "profile-dl-currency", "profile-dl-country"):
        assert f'id="{dl}"' in body
    assert 'id="profile-preview"' in body
    assert 'id="profile-dynamic"' in body
    assert 'id="profile-save-status"' in body
    assert "Built-in template" in body


def test_folder_table_columns():
    body = _page()
    for col in ("<th>Name</th>", "<th>Person</th>", "<th>Language</th>",
                "<th>Units</th>", "<th>Time</th>", "<th>Country</th>"):
        assert col in body


def test_page_has_tree_and_modal_markers():
    body = _page()
    for marker in ('id="profile-tree-root"', 'id="profile-root-drop"',
                   'id="profile-all"', 'id="profile-rename-modal"',
                   'id="profile-new-modal"', 'id="profile-delete-modal"',
                   'id="profile-delete-input"', 'id="profile-node-rename"'):
        assert marker in body, f"missing marker: {marker}"


def test_no_backslash_escapes_in_template():
    # The template is a non-raw Python string: a \n-style escape inside any
    # inline script would be eaten by Python and break the page silently.
    src = Path(profile_views.__file__).read_text(encoding="utf-8")
    template = src.split('PROFILE_TEMPLATE = """', 1)[1].split('"""', 1)[0]
    assert "\\" not in template
```

- [ ] **Step 2:** FAIL → **Step 3:** implement (`/static/profile.js?v=` will 404 until Task 8 — the marker test only checks the reference string, not that it serves) → **Step 4:** PASS (`pytest source/webapp/test_profile_views.py source/webapp/test_prompt_views.py -v` — prompt suite proves no nav regression). Commit: `feat: /profile page shell, nav entry, admin views`

### Task 8: `static/profile.js` — tree port

**Files:** Create `source/static/profile.js`.

Port `static/prompt.js` with the rename table `prompt→profile` / `Prompt→Profile` / `PROMPT_→PROFILE_` / `promptItems→profileItems` / `'prompt.expandedFolders'→'profile.expandedFolders'` / URL `/prompt→/profile` / API `/prompt/api/tree→/profile/api/tree`.

**Keep (mechanical port):** helpers (`EscapeHtml`, `ShortDate`), state, Lucide icon constants, lookups, selection (`SelectFolder/SelectItem/SelectNode/FolderClick`, `SyncUrl`, deep-link init), `RenderTree`/`FolderLi`/`ItemNode` skeleton, kebab machinery (`PlaceMenu`/`MakeKebab`), rename modal, folder description modal, add folder/add item modals, all drag-drop functions, delete modals + cascade, `LoadTree`, toast, `Save`/`SavePush` (250 ms structural debounce), dirty-guarded modal dismissal, wiring + initial paint.

**Drop entirely:** CodeMirror init/wrappers, edit mode, content load/save, diff view, clone, new-chat, `parentUuid`/based-on logic everywhere (items are `{uuid, name, folderId, summary?, builtin?}`).

**Deltas (complete code):**

1. Builtin-aware render order (Templates last at root) + flatten:

```js
function profileRenderTree(){
  document.getElementById('profile-all').className =
    'profile-node' + ((profileSelectedFolder === null && !profileSelectedItem) ? ' sel' : '');
  const root = document.getElementById('profile-tree-root');
  root.innerHTML = '';
  // User content first; the virtual Templates folder renders after it.
  profileChildFolders(null).filter(f => !f.builtin).forEach(f => root.appendChild(profileFolderLi(f)));
  profileItemsInFolder(null).forEach(p => {
    const li = document.createElement('li'); li.appendChild(profileItemNode(p)); root.appendChild(li);
  });
  profileChildFolders(null).filter(f => f.builtin).forEach(f => root.appendChild(profileFolderLi(f)));
}
function profileFlattenTree(parentId){
  parentId = parentId || null;
  const out = [];
  const walk = (f, depth) => {
    out.push({kind: 'folder', node: f, depth: depth});
    profileChildFolders(f.id).forEach(c => walk(c, depth + 1));
    profileItemsInFolder(f.id).forEach(p => out.push({kind: 'item', node: p, depth: depth + 1}));
  };
  if (parentId === null){
    // Same order as the root tree: user folders, user root items, Templates.
    profileChildFolders(null).filter(f => !f.builtin).forEach(f => walk(f, 0));
    profileItemsInFolder(null).forEach(p => out.push({kind: 'item', node: p, depth: 0}));
    profileChildFolders(null).filter(f => f.builtin).forEach(f => walk(f, 0));
  } else {
    profileChildFolders(parentId).forEach(f => walk(f, 0));
    profileItemsInFolder(parentId).forEach(p => out.push({kind: 'item', node: p, depth: 0}));
  }
  return out;
}
```

2. Folder detail rows — six columns from `summary` (`p.summary || {}`: `full_name`, `language`, `units`, `time_format`, `country`) + trailing Open link; folder rows: icon+name, five empty cells, Open. Builtin item name cell appends `<span class="profile-builtin-tag">built-in</span>`.

3. `profileFolderLi` / `profileItemNode`: append the builtin tag on builtin rows; `profileMakeDraggable(...)` only when `!node-data.builtin`; kebab item sets:
   - user folder: `{onRename, onDelete}`; Templates folder: `{}` (see 4)
   - user item: `{onRename, onDuplicate, onDelete}`; builtin item: `{onDuplicate}`

4. `profileMakeKebab(node, opts)`: items list order Rename / Duplicate / Delete(danger); when the list is empty, `kebab.classList.add('profile-kebab-none')` (element still rendered → constant row height).

5. Drop guards: in `profileMakeFolderDrop`'s `okFor`, first line `const tf = profileFolderById(folderId); if (tf && tf.builtin) return false;`. Do not call `profileMakeItemDrop` on builtin items. Cycle guard unchanged.

6. Structural PUT projects to structural keys only, omitting builtins and summary, and records failure for the duplicate flow:

```js
let profileTreeSaveOk = true;
async function profileSavePush(){
  if (profileSaveInFlight){ profileSaveQueued = true; return; }
  profileSaveInFlight = true;
  try {
    const r = await fetch('/profile/api/tree', {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        folders: profileFolders.filter(f => !f.builtin).map(f => ({
          id: f.id, name: f.name, description: f.description || '',
          parentId: f.parentId || null})),
        profiles: profileItems.filter(p => !p.builtin).map(p => ({
          uuid: p.uuid, name: p.name, folderId: p.folderId || null})),
        version: profileTreeVersion, deletes: profilePendingDeletes}),
    });
    const j = await r.json().catch(() => null);
    if (r.status === 409){
      await profileLoadTree();
      profilePendingDeletes = 0;
      profileTreeSaveOk = false;
      if (profileSelectedItem && !profileByUuid(profileSelectedItem)) profileSelectedItem = null;
      if (profileSelectedFolder && !profileFolderById(profileSelectedFolder)) profileSelectedFolder = null;
      profileRenderTree();
      profileRender();
      profileToastMsg('Profile tree was changed elsewhere — reloaded. Your last edit was not saved.');
    } else if (!r.ok){
      profileTreeSaveOk = false;
      profileToastMsg('Save refused: ' + ((j && j.error) || ('HTTP ' + r.status)));
    } else {
      profileTreeVersion = (j && j.version) || profileTreeVersion;
      profilePendingDeletes = 0;
      profileTreeSaveOk = true;
    }
  } catch (e) {
    profileTreeSaveOk = false;
  } finally {
    profileSaveInFlight = false;
    if (profileSaveQueued){ profileSaveQueued = false; profileSavePush(); }
  }
}
```

7. `profileRenderRename`: builtin nodes get a plain `<span>` heading (no rename modal, no hover affordance); user nodes keep the click-to-rename button. `profileRenderFolderDesc`: builtin folder shows description text without the Edit button.

8. `profileAddProfileConfirm` (was AddPromptConfirm): `folderId` = `profileSelectedFolder` unless that folder is builtin → `null`; pushes `{uuid, name, folderId, summary: {}}`; flush-save then `profileSelectItem(uuid)`. Delete messages say profile/profiles.

9. `profileRender()` calls `profileRenderRename / profileRenderFolderDesc / profileRenderContents / profileRenderForm / profileSyncUrl` (form pane in Task 9 — land a stub `function profileRenderForm(){}` here).

- [ ] **Step 1:** Write the file. **Step 2:** `pytest source/webapp/test_profile_views.py source/webapp/test_profile_api.py -v` → PASS. **Step 3:** quick smoke: run the app, load `/profile`, confirm the tree renders with Templates last (full browser acceptance is Task 10). Commit: `feat: /profile tree JS (port of prompt.js, builtin-aware)`

### Task 9: `static/profile.js` — form pane, autosave, duplicate

**Files:** Modify `source/static/profile.js` (replace the stub); Test: append JS marker tests to `webapp/test_profile_views.py`.

Complete code for the new sections — datalists, form fill/read, dynamic group, preview, autosave, duplicate — as drafted below. Key rules from the proposal: 400 ms debounce per profile uuid, one in-flight PUT per profile, queued re-send carries the newest snapshot, late GET discarded unless uuid still selected, status Saving…/Saved ✓/Save failed — retrying, capped exponential backoff retrying while the page is open (immediate on next edit / `online`), `beforeunload` guard only while pending/failed, summary refreshed from the PUT ack, duplicate flushes tree then (user-owned) data saves and aborts visibly on failure.

```js
// ---- datalists (static arrays; timezones from the runtime — no list to maintain) ----
const PROFILE_DL_LANG = ['da','de','en','en-AU','en-CA','en-GB','en-IN','en-SG','en-US','es','es-MX','fr','fr-CA','he','it','ja','ko','nl','pl','pt-BR','sv','te','zh','zh-Hans','zh-Hant'];
const PROFILE_DL_CURRENCY = ['AUD','BRL','CAD','CHF','CNY','DKK','EUR','GBP','ILS','INR','JPY','KRW','MXN','NOK','PLN','SEK','SGD','USD'];
const PROFILE_DL_COUNTRY = ['Australia','Brazil','Canada','China','Denmark','France','Germany','India','Israel','Italy','Japan','Mexico','Netherlands','Poland','Singapore','South Korea','Spain','Sweden','UK','US'];
function profileFillDatalist(id, values){
  const dl = document.getElementById(id);
  dl.innerHTML = '';
  values.forEach(v => { const o = document.createElement('option'); o.value = v; dl.appendChild(o); });
}
function profileInitDatalists(){
  profileFillDatalist('profile-dl-lang', PROFILE_DL_LANG);
  profileFillDatalist('profile-dl-currency', PROFILE_DL_CURRENCY);
  profileFillDatalist('profile-dl-country', PROFILE_DL_COUNTRY);
  let zones = [];
  // Without Intl.supportedValuesOf the timezone input stays free text over an empty list.
  try { if (Intl.supportedValuesOf) zones = Intl.supportedValuesOf('timeZone'); } catch (e) {}
  profileFillDatalist('profile-dl-tz', zones);
}

// ---- form pane ----
const PROFILE_FIELD_KEYS = Array.from(
  document.querySelectorAll('#profile-form [data-key]')).map(el => el.dataset.key);
function profileFieldEl(key){
  return document.querySelector('#profile-form [data-key="' + key + '"]');
}
let profileFormUuid = null;   // uuid whose data the form currently holds

function profileRenderForm(){
  const el = document.getElementById('profile-form');
  const table = document.getElementById('profile-table-wrap');
  const p = profileSelectedItem ? profileByUuid(profileSelectedItem) : null;
  table.hidden = !!p;
  if (!p){ el.hidden = true; profileFormUuid = null; return; }
  el.hidden = false;
  document.getElementById('profile-builtin-hint').hidden = !p.builtin;
  if (profileFormUuid !== p.uuid){
    profileFormUuid = p.uuid;
    profileFillForm({});
    profileSetFormDisabled(true);   // until the data arrives (or stays disabled: builtin)
    profileRenderDynamic(null);
    profileLoadData(p.uuid);
  }
  profileRenderStatus();
}
async function profileLoadData(uuid){
  let d = null;
  try {
    const r = await fetch('/profile/api/profiles/' + encodeURIComponent(uuid));
    d = await r.json();
  } catch (e) { /* handled below */ }
  // A late GET is discarded unless its uuid is still the selected profile.
  if (profileFormUuid !== uuid || profileSelectedItem !== uuid) return;
  const st = profileFormState[uuid];
  if (st && st.snapshot && (st.dirty || st.inFlight || st.failed)){
    // A pending local edit outranks the fetched snapshot — show what autosave will push.
    profileFillForm(st.snapshot);
    profileSetFormDisabled(false);
    return;
  }
  if (!d || !d.ok){
    // A just-created profile may not be saved yet; its data is {} by
    // construction, so the blank form is correct. Enable editing.
    profileSetFormDisabled(false);
    return;
  }
  profileFillForm(d.data || {});
  profileSetFormDisabled(!!d.builtin);
  profileRenderDynamic((d.data && d.data.dynamic) || null);
}
function profileFillForm(data){
  PROFILE_FIELD_KEYS.forEach(k => {
    profileFieldEl(k).value = (data && data[k] != null) ? data[k] : '';
  });
  profileUpdatePreview();
}
function profileReadForm(){
  // Complete editable snapshot; blanks stay off (the server canonicalizes anyway).
  const out = {};
  PROFILE_FIELD_KEYS.forEach(k => {
    const v = profileFieldEl(k).value;
    if (v !== '') out[k] = v;
  });
  return out;
}
function profileSetFormDisabled(dis){
  PROFILE_FIELD_KEYS.forEach(k => { profileFieldEl(k).disabled = dis; });
}
// Connector-written observations: read-only "Last seen" group, only when present.
function profileRenderDynamic(dyn){
  const fs = document.getElementById('profile-dynamic');
  const box = document.getElementById('profile-dynamic-rows');
  box.innerHTML = '';
  const keys = (dyn && typeof dyn === 'object') ? Object.keys(dyn) : [];
  fs.hidden = !keys.length;
  keys.forEach(k => {
    const e = dyn[k] || {};
    const div = document.createElement('div');
    div.className = 'profile-dynamic-row muted';
    const val = (e.value != null) ? String(e.value) : JSON.stringify(e);
    div.textContent = k + ': ' + val + (e.seen_at ? ' — seen ' + profileShortDate(e.seen_at) : '');
    box.appendChild(div);
  });
}

// ---- datetime preview (the preview is the documentation) ----
function profileFormatDateParts(parts, fmt){
  switch (fmt){
    case 'DD/MM/YYYY': return parts.day + '/' + parts.month + '/' + parts.year;
    case 'MM/DD/YYYY': return parts.month + '/' + parts.day + '/' + parts.year;
    case 'DD.MM.YYYY': return parts.day + '.' + parts.month + '.' + parts.year;
    case 'DD-MM-YYYY': return parts.day + '-' + parts.month + '-' + parts.year;
    default: return parts.year + '-' + parts.month + '-' + parts.day;   // YYYY-MM-DD
  }
}
function profileUpdatePreview(){
  const el = document.getElementById('profile-preview');
  const tz = profileFieldEl('timezone').value.trim();
  const dateFmt = profileFieldEl('date_format').value || 'YYYY-MM-DD';
  const hour12 = (profileFieldEl('time_format').value || '24h') === '12h';
  try {
    // Blank timezone = the browser's local zone; an invalid or half-typed
    // zone throws here and must never break the rest of the form.
    const opts = {year: 'numeric', month: '2-digit', day: '2-digit',
                  hour: '2-digit', minute: '2-digit', hour12: hour12};
    if (tz) opts.timeZone = tz;
    const parts = {};
    new Intl.DateTimeFormat('en-GB', opts).formatToParts(new Date())
      .forEach(p => { parts[p.type] = p.value; });
    const time = parts.hour + ':' + parts.minute +
      (parts.dayPeriod ? ' ' + parts.dayPeriod.toLowerCase() : '');
    el.textContent = 'Preview: ' + profileFormatDateParts(parts, dateFmt) + ' · ' + time;
  } catch (e) {
    el.textContent = 'Preview unavailable — timezone not recognized';
  }
}

// ---- autosave (debounced per profile; one in-flight PUT per profile) ----
const PROFILE_SAVE_DEBOUNCE_MS = 400;
const PROFILE_RETRY_MAX_MS = 30000;
let profileFormState = {};   // uuid -> {timer, retryTimer, retryDelay, inFlight, dirty, failed, snapshot}
function profileFormStateFor(uuid){
  if (!profileFormState[uuid]){
    profileFormState[uuid] = {timer: null, retryTimer: null, retryDelay: 1000,
                              inFlight: false, dirty: false, failed: false, snapshot: null};
  }
  return profileFormState[uuid];
}
function profileFieldEdited(){
  const p = profileFormUuid ? profileByUuid(profileFormUuid) : null;
  if (!p || p.builtin) return;
  const uuid = profileFormUuid;
  const st = profileFormStateFor(uuid);
  st.snapshot = profileReadForm();
  st.dirty = true;
  if (st.retryTimer){ clearTimeout(st.retryTimer); st.retryTimer = null; }  // an edit retries a failure immediately
  clearTimeout(st.timer);
  st.timer = setTimeout(() => { st.timer = null; profileDataPush(uuid); },
                        PROFILE_SAVE_DEBOUNCE_MS);
  profileUpdatePreview();
  profileRenderStatus();
}
async function profileDataPush(uuid){
  const st = profileFormStateFor(uuid);
  if (st.timer){ clearTimeout(st.timer); st.timer = null; }
  if (st.inFlight || !st.dirty || !st.snapshot) return;  // the ack handler re-sends queued edits
  st.inFlight = true;
  st.dirty = false;   // a new edit mid-flight re-marks it; failure below restores it
  const snapshot = st.snapshot;
  profileRenderStatus();
  let ok = false, d = null;
  try {
    const r = await fetch('/profile/api/profiles/' + encodeURIComponent(uuid), {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({data: snapshot}),
    });
    d = await r.json().catch(() => null);
    ok = r.ok;
  } catch (e) { /* ok stays false */ }
  st.inFlight = false;
  if (ok){
    st.failed = false;
    st.retryDelay = 1000;
    // Refresh the row's local summary so a folder table opened later shows
    // the saved values without reloading the whole tree.
    const row = profileByUuid(uuid);
    if (row && d && d.summary){ row.summary = d.summary; profileTouch(row); }
    if (st.dirty) profileDataPush(uuid);   // queued re-send with the newest snapshot
  } else {
    st.dirty = true;    // retain the dirty snapshot
    st.failed = true;
    st.retryTimer = setTimeout(() => { st.retryTimer = null; profileDataPush(uuid); },
                               st.retryDelay);
    st.retryDelay = Math.min(st.retryDelay * 2, PROFILE_RETRY_MAX_MS);  // capped; retries while the page is open
  }
  profileRenderStatus();
}
function profileRenderStatus(){
  const el = document.getElementById('profile-save-status');
  const st = profileFormUuid ? profileFormState[profileFormUuid] : null;
  if (!st){ el.textContent = ''; return; }
  if (st.failed) el.textContent = 'Save failed — retrying';
  else if (st.inFlight || st.dirty || st.timer) el.textContent = 'Saving…';
  else if (st.snapshot) el.textContent = 'Saved ✓';
  else el.textContent = '';
}
function profileAnySavePending(){
  return Object.keys(profileFormState).some(u => {
    const st = profileFormState[u];
    return st && (st.dirty || st.inFlight || st.failed || st.timer);
  });
}
// The unload guard is active only while a save is pending or failed; it is
// gone the moment the latest snapshot is acknowledged.
window.addEventListener('beforeunload', (e) => {
  if (profileAnySavePending()){ e.preventDefault(); e.returnValue = ''; }
});
window.addEventListener('online', () => {
  Object.keys(profileFormState).forEach(u => {
    const st = profileFormState[u];
    if (st && st.failed && !st.inFlight){
      if (st.retryTimer){ clearTimeout(st.retryTimer); st.retryTimer = null; }
      profileDataPush(u);
    }
  });
});
// Cancel the debounce and await the newest data PUT; false if it can't be saved.
async function profileFlushData(uuid){
  const st = profileFormState[uuid];
  if (!st) return true;
  if (st.timer){ clearTimeout(st.timer); st.timer = null; }
  if (st.retryTimer){ clearTimeout(st.retryTimer); st.retryTimer = null; }
  while (st.dirty || st.inFlight){
    if (st.inFlight){
      await new Promise(res => setTimeout(res, 50));
    } else {
      await profileDataPush(uuid);
      if (st.failed) return false;
    }
  }
  return !st.failed;
}

// ---- duplicate (kebab) — the one-action way to mint a profile from an archetype ----
async function profileDuplicateUuid(uuid){
  // Flush pending structural edits first: the source row must exist server-side
  // and the new row bumps the version a queued stale tree PUT would 409 on.
  clearTimeout(profileSaveTimer);
  await profileSavePush();
  if (!profileTreeSaveOk){
    profileToastMsg('Duplicate aborted — the tree could not be saved.');
    return;
  }
  const p = profileByUuid(uuid);
  if (p && !p.builtin){
    // An edit followed immediately by Duplicate must be part of the copy.
    const flushed = await profileFlushData(uuid);
    if (!flushed){
      profileToastMsg('Duplicate aborted — the latest edits could not be saved.');
      return;
    }
  }
  let d = null;
  try {
    const r = await fetch('/profile/api/profiles/' + encodeURIComponent(uuid) + '/duplicate',
                          {method: 'POST'});
    d = await r.json();
  } catch (e) { /* handled below */ }
  if (!d || !d.ok){
    profileToastMsg('Duplicate failed: ' + ((d && d.error) || 'server unreachable'));
    return;
  }
  await profileLoadTree();
  profileSelectItem(d.profile.uuid);
}
```

Wiring additions at the bottom of the file (with the existing init):

```js
profileInitDatalists();
document.querySelectorAll('#profile-form [data-key]').forEach(el => {
  el.addEventListener('input', profileFieldEdited);
  el.addEventListener('change', profileFieldEdited);
});
```

Marker tests to append to `webapp/test_profile_views.py` (body = page + served JS, like `test_prompt_views._body`):

```python
def _body() -> str:
    client = app.test_client()
    page = client.get("/profile").get_data(as_text=True)
    js = client.get("/static/profile.js")
    assert js.status_code == 200
    return page + js.get_data(as_text=True)


def test_js_has_core_markers():
    b = _body()
    for marker in ["profileLoadTree", "profileRenderTree", "profileItemNode",
                   "profileSavePush", "profileDataPush", "profileFieldEdited",
                   "profileDuplicateUuid", "profileUpdatePreview",
                   "profileFlushData", "profileRenderDynamic",
                   "/profile/api/tree", "Intl.supportedValuesOf",
                   "Preview unavailable", "beforeunload"]:
        assert marker in b, f"missing JS marker: {marker}"


def test_rename_goes_through_confirm_modal():
    b = _body()
    for marker in ["profile-rename-display", "function profileOpenRenameModal",
                   "function profileConfirmRenameModal"]:
        assert marker in b


def test_tree_rows_are_real_links():
    b = _body()
    assert "node.href = '/profile?id=' + encodeURIComponent(f.id)" in b
    assert "n.href = '/profile?id=' + encodeURIComponent(p.uuid)" in b
    assert '<a class="profile-node" id="profile-all" href="/profile">' in b
    assert "if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;" in b
```

- [ ] **Step 1:** failing marker tests → **Step 2:** implement → **Step 3:** full `pytest source/db/test_profile_tree.py source/webapp/test_profile_api.py source/webapp/test_profile_views.py -v` → PASS. Commit: `feat: /profile form pane — autosave, preview, datalists, duplicate`

### Task 10: Browser acceptance + docs

- [ ] **Step 1:** Run the app against `rainbox_claude` (`DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude python source/main.py` or the project's run skill) and verify in a real browser (headless Chrome + CDP is fine) per tree-doc §8: drag a profile to the root strip; kebab on the selected row only; type-to-confirm folder delete; create → edit fields → autosave status reaches "Saved ✓"; reload → values round-trip; duplicate right after an edit copies the edit; half-typed timezone shows "Preview unavailable…" and the form keeps working; built-ins: disabled fields, hint line, kebab = Duplicate only, not draggable, tree save leaves them untouched; deep link `?id=<template-uuid>` works.
- [ ] **Step 2:** Docs (current state only): add `/profile` to `docs/ui-left-panel-tree.md`'s page list (§0 intro + reference note that its leaf pane is a form) and to `docs/ui-modal-rename.md` "Where it applies"; flip the proposal's status line to `**Status: implemented.**`.
- [ ] **Step 3:** Full test suite for the touched areas: `pytest source/db source/webapp -x -q` (pre-existing failures per memory are not ours — compare against `main` if anything fails). Commit: `docs: /profile joins the tree + rename-modal docs; proposal implemented`

## Self-review notes

- Spec coverage: page structure (T7/T8), data model + registry (T1), validation semantics (T2), tree persistence + summary/version split (T3), built-ins virtual + guards (T4), data ops + dynamic merge + duplicate placement (T5), API incl. read-only built-in 400s (T6), form + autosave + preview + datalists + unload guard + duplicate flush (T9), acceptance + docs (T10). All seven proposal test groups are distributed across T1–T9 test steps.
- Type consistency: `summary` is always the five-key object from `profile_data_summary`; duplicate/tree rows share `{uuid, name, folderId, summary}`; `builtin` appears only on built-in rows.
