"""Shared app instance, DB init, model_config sync, and Flask-Admin setup.

Imported first by webapp/__init__.py so that `app` and `benchmark_runner`
exist before the view modules register their routes against them.
"""

import json
import logging
from datetime import datetime
from uuid import UUID

from flask_admin import Admin
from flask_admin.contrib.sqla import ModelView
from flask_admin.menu import MenuLink
from flask_admin.model.typefmt import BASE_FORMATTERS
from flask_admin.theme import Bootstrap4Theme
from jinja2 import ChoiceLoader, DictLoader
from markupsafe import Markup, escape
from wtforms import StringField, TextAreaField

from benchmarks.runner import BenchmarkRunner
from db import (
    AgentModelBinding,
    AppSetting,
    AssistantControl,
    AssistantRun,
    AssistantStep,
    AssistantWriteIntent,
    ChatMessage,
    Chatroom,
    ChatroomFolder,
    ChatroomMember,
    ChatUser,
    ConversationRun,
    CronFolder,
    CronJob,
    CronRun,
    EvalCase,
    EvalResult,
    EvalRun,
    FeedbackEvent,
    GitFolder,
    GitRepo,
    Inbox,
    Journal,
    KanbanBoard,
    KanbanBoardFolder,
    KanbanColumn,
    KanbanTask,
    KanbanTaskEvent,
    MemoryClaim,
    MemoryEmbedding,
    MemoryEvidence,
    ModelConfig,
    ModelConfigOverride,
    ModelGroup,
    ModelGroupMember,
    SeedMemoryKb,
    RetrievalEvent,
    WorkspaceShellState,
    db,
    init_db,
    make_app,
    sync_model_configs,
)
import providers

_log = logging.getLogger(__name__)

app = make_app()
init_db(app)

# Shared top nav, registered as an includable template so every page can do
# {% include "_nav.html" %} and stay in sync. Active link is derived from
# request.endpoint. The "Admin Panel" button is pushed right by the spacer.
# /modelgrouppriorities deliberately does NOT include this — it has its own
# "× Close" button instead.
NAV_TEMPLATE = """
<style>
  .pp-nav{position:sticky;top:0;z-index:100;display:flex;align-items:center;gap:18px;padding:10px 20px;margin-bottom:1.5em;
          border-bottom:1px solid #e5e7eb;background:#fff;font-family:system-ui,sans-serif}
  .pp-nav a{color:#6c757d;text-decoration:none;font-size:0.92rem;font-weight:500}
  .pp-nav a:hover{color:#1a1a2e}
  .pp-nav a.pp-active{color:#1a1a2e;font-weight:600}
  .pp-nav .pp-links{display:flex;align-items:center;gap:18px;flex:1 1 auto;flex-wrap:wrap}
  .pp-nav .pp-spacer{flex:1 1 auto}
  .pp-nav a.pp-admin{padding:6px 16px;background:#2563eb;color:#fff;border-radius:8px}
  .pp-nav a.pp-admin:hover{background:#1d4ed8;color:#fff}
  .pp-content{padding:0 24px 2em}
  /* Benchmark dropdown — native <details>, no JS. */
  .pp-nav details.pp-dd{position:relative}
  .pp-nav details.pp-dd>summary{list-style:none;cursor:pointer;color:#6c757d;font-size:0.92rem;font-weight:500;
          -webkit-user-select:none;user-select:none}
  .pp-nav details.pp-dd>summary::-webkit-details-marker{display:none}
  .pp-nav details.pp-dd>summary:hover{color:#1a1a2e}
  .pp-nav details.pp-dd[open]>summary,.pp-nav details.pp-dd.pp-active>summary{color:#1a1a2e;font-weight:600}
  .pp-nav details.pp-dd>.pp-dd-menu{position:absolute;top:100%;left:0;margin-top:8px;background:#fff;border:1px solid #e5e7eb;
          border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,0.10);padding:6px;min-width:170px;display:flex;flex-direction:column;gap:2px;z-index:200}
  .pp-nav details.pp-dd>.pp-dd-menu a{padding:7px 10px;border-radius:6px;white-space:nowrap}
  .pp-nav details.pp-dd>.pp-dd-menu a:hover{background:#f1f5f9}
  /* Hamburger toggle — hidden on wide screens, collapses the links on narrow. */
  .pp-nav .pp-burger{display:none;background:none;border:1px solid #e5e7eb;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:1.15rem;line-height:1;color:#374151}
  @media (max-width:820px){
    .pp-nav{flex-wrap:wrap}
    .pp-nav .pp-burger{display:block;margin-left:auto}
    .pp-nav .pp-links{display:none;flex-direction:column;align-items:flex-start;width:100%;gap:12px;padding-top:10px}
    .pp-nav.pp-open .pp-links{display:flex}
    .pp-nav .pp-spacer{display:none}
    .pp-nav details.pp-dd>.pp-dd-menu{position:static;box-shadow:none;border:none;margin:2px 0 0 14px;padding:0;min-width:0}
  }
</style>
<nav class="pp-nav" id="pp-nav">
  <a href="{{ url_for('index') }}" class="{{ 'pp-active' if request.endpoint == 'index' }}">Home</a>
  <button type="button" class="pp-burger" aria-label="Toggle navigation"
          onclick="this.closest('.pp-nav').classList.toggle('pp-open')">&#9776;</button>
  <div class="pp-links">
    <a href="{{ url_for('chat_page') }}" class="{{ 'pp-active' if request.endpoint == 'chat_page' }}">Chat</a>
    <a href="{{ url_for('conversation_page') }}" class="{{ 'pp-active' if request.endpoint == 'conversation_page' }}">Conversations</a>
    <a href="{{ url_for('cron_page') }}" class="{{ 'pp-active' if request.endpoint == 'cron_page' }}">Cron</a>
    <a href="{{ url_for('kanban_page') }}" class="{{ 'pp-active' if request.endpoint == 'kanban_page' }}">Kanban</a>
    <a href="{{ url_for('memory_page') }}" class="{{ 'pp-active' if request.endpoint == 'memory_page' }}">Memory</a>
    <a href="{{ url_for('git_page') }}" class="{{ 'pp-active' if request.endpoint == 'git_page' }}">Git</a>
    <a href="{{ url_for('settings_page') }}" class="{{ 'pp-active' if request.endpoint == 'settings_page' }}">Settings</a>
    <details class="pp-dd {{ 'pp-active' if request.endpoint in ('models_page', 'modelgroups_page', 'agent_models_page') }}">
      <summary>Models &#9662;</summary>
      <div class="pp-dd-menu">
        <a href="{{ url_for('models_page') }}" class="{{ 'pp-active' if request.endpoint == 'models_page' }}">Configs</a>
        <a href="{{ url_for('modelgroups_page') }}" class="{{ 'pp-active' if request.endpoint == 'modelgroups_page' }}">Groups</a>
        <a href="{{ url_for('agent_models_page') }}" class="{{ 'pp-active' if request.endpoint == 'agent_models_page' }}">Agent models</a>
      </div>
    </details>
    <details class="pp-dd {{ 'pp-active' if request.endpoint in ('benchmark_basic_page', 'benchmark_editdocument_page', 'benchmark_kanban_page') }}">
      <summary>Benchmark &#9662;</summary>
      <div class="pp-dd-menu">
        <a href="{{ url_for('benchmark_basic_page') }}" class="{{ 'pp-active' if request.endpoint == 'benchmark_basic_page' }}">Basic</a>
        <a href="{{ url_for('benchmark_editdocument_page') }}" class="{{ 'pp-active' if request.endpoint == 'benchmark_editdocument_page' }}">Edit document</a>
        <a href="{{ url_for('benchmark_kanban_page') }}" class="{{ 'pp-active' if request.endpoint == 'benchmark_kanban_page' }}">Kanban</a>
      </div>
    </details>
    <details class="pp-dd {{ 'pp-active' if request.endpoint in ('demo_tts_kokoro', 'demo_stt_whisper', 'demo_voice_echo') }}">
      <summary>Voice &#9662;</summary>
      <div class="pp-dd-menu">
        <a href="{{ url_for('demo_tts_kokoro') }}" class="{{ 'pp-active' if request.endpoint == 'demo_tts_kokoro' }}">TTS</a>
        <a href="{{ url_for('demo_stt_whisper') }}" class="{{ 'pp-active' if request.endpoint == 'demo_stt_whisper' }}">STT</a>
        <a href="{{ url_for('demo_voice_echo') }}" class="{{ 'pp-active' if request.endpoint == 'demo_voice_echo' }}">Echo</a>
      </div>
    </details>
    <a href="{{ url_for('doctor_page') }}" class="{{ 'pp-active' if request.endpoint == 'doctor_page' }}">Doctor</a>
    <span class="pp-spacer"></span>
    <a href="{{ url_for('admin.index') }}" class="pp-admin">Admin Panel</a>
  </div>
</nav>
<script>
  // Native <details> doesn't auto-close, so dismiss any open nav dropdown when
  // a click (or Escape) lands outside it.
  document.addEventListener('click', function(e) {
    document.querySelectorAll('nav.pp-nav details.pp-dd[open]').forEach(function(d) {
      if (!d.contains(e.target)) d.removeAttribute('open');
    });
  });
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      document.querySelectorAll('nav.pp-nav details.pp-dd[open]').forEach(function(d) {
        d.removeAttribute('open');
      });
    }
  });
</script>
"""

_existing_loader = app.jinja_env.loader
app.jinja_env.loader = ChoiceLoader(
    [DictLoader({"_nav.html": NAV_TEMPLATE})]
    + ([_existing_loader] if _existing_loader is not None else [])
)

benchmark_runner = BenchmarkRunner()
kanban_benchmark_runner = BenchmarkRunner(spec_set="kanban")

from benchmarks.editdocument_runner import BenchmarkEditDocumentRunner
benchmark_editdocument_runner = BenchmarkEditDocumentRunner(app)


def _sync_one_provider(
    prov: providers.Provider,
    force_update_arguments: bool,
) -> dict | None:
    """Reconcile model_config rows for a single provider. Returns the
    summary dict, or None if the provider is unreachable (in which case
    that provider's rows are left untouched)."""
    try:
        available = prov.list_models()
    except Exception as e:
        _log.warning(
            "model_config sync skipped — could not reach %s at %s: %s",
            prov.display_name, prov.base_url(), e,
        )
        return None
    sizes = prov.fetch_model_sizes()
    # Capability flags from the provider's native API. None => unreachable,
    # in which case we leave is_function_calling_model untouched (don't
    # clobber to False).
    native = prov.fetch_native_models()
    func_calling = (
        {
            m["id"]: ("tool_use" in (m.get("capabilities") or []))
            for m in native
            if m.get("id")
        }
        if native is not None
        else None
    )
    summary = sync_model_configs(
        provider=prov.id,
        available_model_names=available,
        default_arguments=prov.default_arguments(),
        sizes_by_name=sizes,
        function_calling_by_name=func_calling,
        force_update_arguments=force_update_arguments,
    )
    _log.info(
        "model_config sync (%s — %d models, %d sizes, "
        "%d with capabilities, force=%s): %s",
        prov.display_name, len(available), len(sizes),
        len(func_calling) if func_calling else 0,
        force_update_arguments, summary,
    )
    return summary


def sync_models_from_providers(
    force_update_arguments: bool = False,
) -> dict[str, dict | None]:
    """Reconcile model_config rows against every registered provider.
    Returns {provider_id: summary_or_None}. None means the provider was
    unreachable — its rows are left untouched (not flipped to
    unavailable). Must run inside an app context."""
    return {
        prov.id: _sync_one_provider(prov, force_update_arguments)
        for prov in providers.all_providers()
    }


with app.app_context():
    sync_models_from_providers()


admin = Admin(
    app,
    name="rainbox",
    url="/admin",
    theme=Bootstrap4Theme(fluid=True),
)
admin.add_link(MenuLink(name="Dashboard", url="/"))


def _format_app_setting_value(view, context, model, name):
    # Never render a secret's value in the admin list (redaction mirrors
    # db_settings.all_settings()).
    if model.secret and model.value not in (None, ""):
        return Markup("<i>••••••</i>")
    return model.value


class AppSettingView(ModelView):
    # Read-only on purpose: writes must go through db.set_setting() so the
    # registry's coercion/validation runs and value_type/secret/description stay
    # in sync. A raw editable table would let those drift or hold invalid values.
    # Editing arrives later via a dedicated /settings page.
    can_create = False
    can_edit = False
    can_delete = False
    column_list = ("key", "value", "value_type", "secret", "description", "updated_at")
    column_default_sort = "key"
    column_formatters = {"value": _format_app_setting_value}


admin.add_view(AppSettingView(AppSetting, db, category="Config"))
admin.add_view(ModelView(Inbox, db))
admin.add_view(ModelView(ModelConfig, db, category="Config"))
# JournalView is registered later, once _fmt_short_uuid is defined.


def _format_model_config_ref(view, context, model, name):
    ref = model.model_config_ref
    uuid_str = escape(str(model.model_config_uuid))
    if ref is None:
        return Markup(f"<i>(missing)</i><br><code>{uuid_str}</code>")
    return Markup(f"<b>{escape(ref.model_name)}</b><br><code>{uuid_str}</code>")


class ModelConfigOverrideView(ModelView):
    column_list = (
        "id",
        "uuid",
        "display_name",
        "model_config_uuid",
        "overrides",
        "created_at",
        "updated_at",
    )
    column_labels = {"model_config_uuid": "Model"}
    column_formatters = {"model_config_uuid": _format_model_config_ref}


class ChatroomMemberView(ModelView):
    # chatroom_member is entirely PK + FK columns, which Flask-Admin auto-hides
    # from the scaffolded list/form — leaving the view empty. List them
    # explicitly (and show the PK) so the table is usable.
    column_display_pk = True
    column_list = ("id", "room_uuid", "user_uuid")
    form_columns = ("room_uuid", "user_uuid")


class WorkspaceShellStateView(ModelView):
    # room_uuid is the PK (auto-hidden by the scaffold) — show it so each row is
    # identifiable by its chatroom.
    column_display_pk = True
    column_list = ("room_uuid", "cwd", "env", "updated_at")


def _format_room_name(view, context, model, name):
    room = db.session.query(Chatroom).filter_by(uuid=model.room_uuid).first()
    return room.name if room else str(model.room_uuid)


def _format_sender_name(view, context, model, name):
    # sender_uuid references chat_user.uuid (not its integer PK), so look it up
    # by the uuid column rather than via session.get (which uses the PK).
    user = db.session.query(ChatUser).filter_by(uuid=model.sender_uuid).first()
    return user.name if user else str(model.sender_uuid)


def _resolve_debug_assistant_text(model) -> str | None:
    """A debug-assistant row stores only a {run_id, step_index, summary} pointer.
    Resolve it to the step's full action/reason/args/observation (no truncation),
    or None if `model` is not a resolvable debug-assistant row."""
    if model.kind != "debug-assistant":
        return None
    try:
        ptr = json.loads(model.text or "")
        run_id, step_index = ptr.get("run_id"), ptr.get("step_index")
    except (ValueError, TypeError):
        return None
    if run_id is None:
        return None
    steps = (db.session.query(AssistantStep)
             .filter_by(run_id=run_id, step_index=step_index)
             .order_by(AssistantStep.id).all())
    if not steps:
        return None
    decision = next((s for s in steps if s.action), steps[0])
    lines = [f"step {step_index} · {decision.action or '?'}"]
    if decision.reason:
        lines.append(decision.reason)
    if decision.args:
        lines.append("args: " + json.dumps(decision.args))      # full, no cap
    for s in steps:
        if s.phase == "observed" and s.observation_preview:
            lines.append("observation: " + s.observation_preview)  # full, no cap
        elif s.phase == "failed":
            lines.append("error: " + (s.error or s.observation_preview or "failed"))
        elif s.phase == "final":
            lines.append("→ replied to the user")
    return "\n".join(lines)


def _format_chatmessage_text(view, context, model, name):
    """List/detail formatter. A debug-assistant row's text is the full step state
    as JSON — render it in a <pre> so indentation/newlines survive.
    (`_resolve_debug_assistant_text` is a fallback for legacy rows that still hold
    a {run_id, step_index} pointer.) Other rows pass through unchanged."""
    text = model.text or ""
    if model.kind != "debug-assistant":
        return text
    shown = _resolve_debug_assistant_text(model) or text
    return Markup(f'<pre style="white-space:pre-wrap;margin:0">{escape(shown)}</pre>')


def _fmt_copyable_uuid(view, context, model, name):
    """A row's own uuid shown in full (monospace) with a one-click Copy button —
    so the operator can grab a precise message reference to share. Unlike the
    truncate-on-hover formatter used for FK columns, the point here is to copy."""
    value = getattr(model, name)
    if not value:
        return ""
    full = escape(str(value))  # a uuid has no HTML-special chars; safe to inline
    return Markup(
        f'<code>{full}</code> '
        f'<button type="button" title="Copy uuid" '
        f"onclick=\"navigator.clipboard.writeText('{full}');"
        f"this.textContent='✓';setTimeout(()=>this.textContent='⧉',1000)\" "
        'style="cursor:pointer;border:1px solid #ccc;border-radius:4px;'
        'background:#fff;font:inherit;padding:0 .35em">⧉</button>'
    )


class SeedMemoryKbView(ModelView):
    # LlamaIndex's PGVectorStore owns this table — read-only here so admins
    # can browse the embedded seed-memory Q&A entries without risking state.
    can_create = False
    can_edit = False
    can_delete = False
    column_display_pk = True
    column_list = ("id", "text", "metadata_", "node_id")
    column_default_sort = ("id", False)


class ChatMessageView(ModelView):
    # Order oldest-first by created_at (chronological). (column, descending=False).
    column_default_sort = ("created_at", False)
    # Flask-Admin hides FK columns (room_uuid) from the scaffold, so list columns
    # explicitly. Show the chatroom and sender by name instead of raw uuids.
    column_list = ("created_at", "uuid", "room_uuid", "sender_uuid", "kind",
                   "content_type", "text")
    column_labels = {"room_uuid": "Room", "sender_uuid": "Sender", "uuid": "UUID"}
    column_formatters = {
        "uuid": _fmt_copyable_uuid,
        "room_uuid": _format_room_name,
        "sender_uuid": _format_sender_name,
        "text": _format_chatmessage_text,
    }
    column_formatters_detail = {"text": _format_chatmessage_text}
    # Flask-Admin's form converter skips the UUID columns, so the edit form shows
    # neither the room/sender uuids nor their names. Add read-only reference
    # fields that show both ("name (uuid)"), filled in on_form_prefill.
    form_extra_fields = {
        "room": StringField("Room", render_kw={"readonly": True}),
        "sender": StringField("Sender", render_kw={"readonly": True}),
        # For a debug-assistant row, show the full resolved step (action/reason/
        # args/observation) read-only — the editable `text` field holds only the
        # raw pointer.
        "resolved": TextAreaField(
            "Resolved trace (debug-assistant)",
            render_kw={"readonly": True, "rows": 24, "style": "width:100%"}),
    }

    def on_form_prefill(self, form, id):
        msg = self.get_one(id)
        if msg is None:
            return
        room = db.session.query(Chatroom).filter_by(uuid=msg.room_uuid).first()
        user = db.session.query(ChatUser).filter_by(uuid=msg.sender_uuid).first()
        form.room.data = f"{room.name if room else '(unknown)'} ({msg.room_uuid})"
        form.sender.data = f"{user.name if user else '(unknown)'} ({msg.sender_uuid})"
        if msg.kind == "debug-assistant":
            form.resolved.data = _resolve_debug_assistant_text(msg) or (msg.text or "")
        else:
            form.resolved.data = "(not a debug-assistant row)"


admin.add_view(ModelConfigOverrideView(ModelConfigOverride, db, category="Config"))


# Shared admin display helpers, used by the Chatroom-folder view just below and
# the Cron/Kanban views further down (defined here so the Chat folder can be
# registered at the head of the Chat menu). `CRON_TYPE_FORMATTERS` keeps the
# datetime cells compact; `_fmt_short_uuid` truncates uuid columns.
def _fmt_cron_datetime(view, value, name):
    """Compact datetime for the admin lists: drop sub-seconds and put a space
    before the timezone (e.g. '2026-06-05 23:57:30 +02:00') so the cell
    word-wraps nicely instead of showing a huge microsecond string."""
    if value is None:
        return ""
    out = value.strftime("%Y-%m-%d %H:%M:%S")
    off = value.strftime("%z")  # e.g. '+0200'
    if off:
        out += " " + off[:3] + ":" + off[3:]  # '+02:00'
    return out


CRON_TYPE_FORMATTERS = dict(BASE_FORMATTERS)
CRON_TYPE_FORMATTERS[datetime] = _fmt_cron_datetime


def _fmt_short_uuid(view, context, model, name):
    """Show only the first 6 chars of a uuid, full value on hover, so the column
    stays narrow."""
    value = getattr(model, name)
    if not value:
        return ""
    full = str(value)
    return Markup(f'<code title="{escape(full)}">{escape(full[:6])}</code>')


class JournalView(ModelView):
    # journal.id is a uuid (not monotonic), so default to chronological order by
    # enqueued_at, newest first — `id` ordering would look random. uuid columns
    # show only the first 6 chars (full value on hover) to keep the row narrow.
    column_default_sort = ("enqueued_at", True)
    column_formatters = {"id": _fmt_short_uuid, "agent_uuid": _fmt_short_uuid}


admin.add_view(JournalView(Journal, db))


# ChatroomFolder backs the /chat left-panel folder tree (folders → rooms). Same
# low-level inspection role as Chatroom itself; the curated surface is the /chat
# page. Registered first below so it heads the Chat menu, above the rooms it
# organizes. parent_uuid is a plain self-referencing column (no FK).
def _chat_folder_label(view, context, model, name):
    """Render a chatroom-folder uuid column (a folder's `parent_uuid`) as a
    truncated uuid (full on hover) on one line and the folder's name below.
    parent_uuid is a plain self-referencing column (no FK), so look it up by
    uuid."""
    fid = getattr(model, name)
    if not fid:
        return ""
    full = str(fid)
    short = Markup(f'<code title="{escape(full)}">{escape(full[:6])}</code>')
    folder = db.session.query(ChatroomFolder).filter_by(uuid=fid).first()
    return Markup(f"{short}<br>{escape(folder.name)}") if folder else short


def _chat_folder_open_link(view, context, model, name):
    """Virtual column linking to /chat deep-linked to this folder. /chat takes a
    single ?id=<uuid> that resolves to either a folder or a room (like /cron)."""
    return Markup(f'<a href="/chat?id={escape(model.uuid)}">inspect ↗</a>')


class ChatroomFolderView(ModelView):
    column_list = ("chat_link", "position", "uuid", "name", "parent_uuid",
                   "created_at", "updated_at")
    column_default_sort = ("position", False)
    column_labels = {"chat_link": "Chat page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "parent_uuid": _chat_folder_label,
        "chat_link": _chat_folder_open_link,
    }


# Chat menu order: folder → room → message first (the everyday browse path),
# then the supporting tables. Flask-Admin orders a category by add_view order.
admin.add_view(ChatroomFolderView(ChatroomFolder, db, category="Chat"))
admin.add_view(ModelView(Chatroom, db, category="Chat"))
admin.add_view(ChatMessageView(ChatMessage, db, category="Chat"))
admin.add_view(ModelView(ChatUser, db, category="Chat"))
admin.add_view(ChatroomMemberView(ChatroomMember, db, category="Chat"))
admin.add_view(WorkspaceShellStateView(WorkspaceShellState, db, category="Chat"))
admin.add_view(SeedMemoryKbView(SeedMemoryKb, db, category="Memory"))


class MemoryClaimView(ModelView):
    column_list = (
        "uuid", "scope", "kind", "status", "sensitivity",
        "confidence", "text", "updated_at",
    )
    # Ascending so the oldest memories sit at the top of page 1 and the
    # newest land on the last page — matches a chat-log reading order.
    column_default_sort = ("updated_at", False)
    # Flask-Admin truncates long text columns in list view automatically;
    # keep `text` last in the list so layout doesn't get squeezed.


class MemoryEvidenceView(ModelView):
    column_list = (
        "uuid", "memory_uuid", "provenance", "source_type",
        "source_id", "created_at",
    )
    # Ascending: oldest evidence rows at the top of page 1, newest at
    # the last page — matches the MemoryClaim list ordering.
    column_default_sort = ("created_at", False)


class FeedbackEventView(ModelView):
    column_list = (
        "created_at", "rating", "room_uuid", "agent_uuid",
        "message_uuid", "comment",
    )
    # Newest feedback first — operators usually want the latest votes.
    column_default_sort = ("created_at", True)


class RetrievalEventView(ModelView):
    can_create = False
    can_edit = False
    can_delete = False
    column_list = (
        "created_at",
        "source",
        "stage",
        "target_type",
        "target_id",
        "query",
        "retrieval_rank",
        "retrieval_score",
        "filter_label",
        "room_uuid",
        "agent_uuid",
        "journal_id",
    )
    column_default_sort = ("created_at", False)  # ascending — stable pagination


class EvalCaseView(ModelView):
    column_list = (
        "created_at", "status", "split", "case_type", "name",
        "source_feedback_uuid",
    )
    # Newest first — operators usually edit the most recent promotion.
    column_default_sort = ("created_at", True)
    # Make the JSON blobs editable in the row form.
    form_columns = (
        "name", "case_type", "split", "status", "source_feedback_uuid",
        "input", "expected", "rubric",
    )


class MemoryEmbeddingView(ModelView):
    """The rainbox-owned half of hybrid retrieval. Read-only (machine-generated)
    and the 768-float `embedding` vector is excluded everywhere — only its
    metadata (claim, model, dim, hash, timestamps) is useful to inspect."""
    can_create = False
    can_edit = False
    column_default_sort = ("created_at", True)
    column_exclude_list = ("embedding",)
    column_details_exclude_list = ("embedding",)


admin.add_view(MemoryClaimView(MemoryClaim, db, category="Memory"))
admin.add_view(MemoryEvidenceView(MemoryEvidence, db, category="Memory"))
admin.add_view(MemoryEmbeddingView(MemoryEmbedding, db, category="Memory"))
admin.add_view(FeedbackEventView(FeedbackEvent, db, category="Feedback"))
admin.add_view(RetrievalEventView(RetrievalEvent, db, category="Telemetry"))
admin.add_view(EvalCaseView(EvalCase, db, category="Feedback"))
# Model-config tables (the Models pages are the curated UI; these expose the raw rows).
admin.add_view(ModelView(ModelGroup, db, category="Config"))
admin.add_view(ModelView(ModelGroupMember, db, category="Config"))
admin.add_view(ModelView(AgentModelBinding, db, category="Config"))
admin.add_view(ModelView(ConversationRun, db, category="Chat"))
# Assistant ReAct loop: runs, per-step trace, control channel, write intents.
admin.add_view(ModelView(AssistantRun, db, category="Assistant"))
admin.add_view(ModelView(AssistantStep, db, category="Assistant"))
admin.add_view(ModelView(AssistantControl, db, category="Assistant"))
admin.add_view(ModelView(AssistantWriteIntent, db, category="Assistant"))


class EvalRunView(ModelView):
    column_list = (
        "started_at", "finished_at", "is_baseline", "name", "agent_role", "summary",
    )
    column_default_sort = ("started_at", True)
    # Allow admins to toggle is_baseline through the row form.
    form_columns = (
        "name", "agent_role", "is_baseline", "config", "summary",
    )


class EvalResultView(ModelView):
    column_list = (
        "created_at", "eval_run_uuid", "eval_case_uuid",
        "score", "passed",
    )
    column_default_sort = ("created_at", True)


admin.add_view(EvalRunView(EvalRun, db, category="Feedback"))
admin.add_view(EvalResultView(EvalResult, db, category="Feedback"))


# Cron tables backing the /cron page (folder tree + jobs, + the future run log).
# The shared display helpers (_fmt_short_uuid / CRON_TYPE_FORMATTERS) are defined
# up by the Chat section so the Chatroom-folder view can reuse them.
def _cron_open_link(view, context, model, name):
    """Virtual column linking to the /cron page deep-linked to this node, so an
    operator can jump from the admin row straight to the folder/job there."""
    return Markup(f'<a href="/cron?id={escape(model.uuid)}">inspect ↗</a>')


def _cron_folder_label(view, context, model, name):
    """Render a folder-uuid column (a job's `folder_uuid` or a folder's
    `parent_uuid`) as a truncated uuid (full value on hover) on one line and the
    folder's name below it. These are plain columns (no FK), so look the folder
    up by uuid."""
    fid = getattr(model, name)
    if not fid:
        return ""
    full = str(fid)
    short = Markup(f'<code title="{escape(full)}">{escape(full[:6])}</code>')
    folder = db.session.query(CronFolder).filter_by(uuid=fid).first()
    return Markup(f"{short}<br>{escape(folder.name)}") if folder else short


class CronFolderView(ModelView):
    column_list = (
        "cron_link", "position", "uuid", "name", "description", "parent_uuid",
        "enabled", "created_at", "updated_at",
    )
    column_default_sort = ("position", False)
    column_labels = {"cron_link": "Cron page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "parent_uuid": _cron_folder_label,
        "cron_link": _cron_open_link,
    }


def _cron_target_label(view, context, model, name):
    """A message job's `target` is a chatroom uuid (rename-proof). Render the
    truncated uuid (full on hover) with the room's current name below it; other
    action types have no target."""
    tgt = (getattr(model, name) or "").strip()
    if not tgt:
        return ""
    try:
        tid = UUID(tgt)
    except (ValueError, TypeError):
        return Markup(f"<code>{escape(tgt)}</code> <i>(not a uuid)</i>")
    short = Markup(f'<code title="{escape(tgt)}">{escape(tgt[:6])}</code>')
    room = db.session.query(Chatroom).filter_by(uuid=tid).first()
    return Markup(f"{short}<br>#{escape(room.name)}") if room else Markup(
        f"{short}<br><i>(unknown room)</i>")


class CronJobView(ModelView):
    column_list = (
        "cron_link", "position", "uuid", "name", "folder_uuid", "enabled",
        "action_type", "cron_expr", "timezone", "command", "target", "description",
        "next_run_at", "created_at", "updated_at",
    )
    column_default_sort = ("position", False)
    column_labels = {"cron_link": "Cron page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "folder_uuid": _cron_folder_label,
        "target": _cron_target_label,
        "cron_link": _cron_open_link,
    }


def _cron_job_path(job: "CronJob") -> str:
    """`folder / nested folder / job name` for a cron job, root-first. Walks the
    folder_uuid -> parent_uuid chain (plain columns, no FK); cycle-guarded."""
    names: list[str] = []
    seen: set = set()
    cur = job.folder_uuid
    while cur and cur not in seen:
        seen.add(cur)
        folder = db.session.query(CronFolder).filter_by(uuid=cur).first()
        if folder is None:
            break
        names.append(folder.name)
        cur = folder.parent_uuid
    names.reverse()  # collected leaf-first; show root-first
    names.append(job.name or "(unnamed)")
    return " / ".join(names)


def _cron_run_job_path(view, context, model, name):
    """Render a CronRun's `cron_uuid` as the truncated uuid (full on hover) on
    one line and the job's folder/.../name path below it. `cron_uuid` is a plain
    column (no FK), so look the job up by uuid."""
    cid = getattr(model, name)
    if not cid:
        return ""
    full = str(cid)
    short = Markup(f'<code title="{escape(full)}">{escape(full[:6])}</code>')
    job = db.session.query(CronJob).filter_by(uuid=cid).first()
    if job is None:
        return Markup(f"{short}<br><i>(deleted job)</i>")
    return Markup(f"{short}<br>{escape(_cron_job_path(job))}")


class CronRunView(ModelView):
    column_list = ("fired_at", "cron_uuid", "trigger", "debug", "journal_id")
    column_default_sort = ("fired_at", False)  # ascending (oldest first)
    column_labels = {"cron_uuid": "Cron job"}
    column_formatters = {"cron_uuid": _cron_run_job_path}


admin.add_view(CronFolderView(CronFolder, db, category="Cron"))
admin.add_view(CronJobView(CronJob, db, category="Cron"))
admin.add_view(CronRunView(CronRun, db, category="Cron"))


# Git tables backing the /git page (folder tree + repos). Same low-level
# inspection role as the Cron/Kanban categories; the curated surface is /git.
def _git_open_link(view, context, model, name):
    """Virtual column linking to the /git page deep-linked to this node, so an
    operator can jump from the admin row straight to the folder/repo there."""
    return Markup(f'<a href="/git?id={escape(model.uuid)}">inspect ↗</a>')


def _git_folder_label(view, context, model, name):
    """Render a folder-uuid column (a repo's `folder_uuid` or a folder's
    `parent_uuid`) as a truncated uuid (full on hover) with the folder's name
    below. Plain columns (no FK), so look the folder up by uuid."""
    fid = getattr(model, name)
    if not fid:
        return ""
    full = str(fid)
    short = Markup(f'<code title="{escape(full)}">{escape(full[:6])}</code>')
    folder = db.session.query(GitFolder).filter_by(uuid=fid).first()
    return Markup(f"{short}<br>{escape(folder.name)}") if folder else short


class GitFolderView(ModelView):
    column_list = (
        "git_link", "position", "uuid", "name", "description", "parent_uuid",
        "created_at", "updated_at",
    )
    column_default_sort = ("position", False)
    column_labels = {"git_link": "Git page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "parent_uuid": _git_folder_label,
        "git_link": _git_open_link,
    }


class GitRepoView(ModelView):
    column_list = (
        "git_link", "position", "uuid", "name", "folder_uuid", "path",
        "description", "created_at", "updated_at",
    )
    column_default_sort = ("position", False)
    column_labels = {"git_link": "Git page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "folder_uuid": _git_folder_label,
        "git_link": _git_open_link,
    }


admin.add_view(GitFolderView(GitFolder, db, category="Git"))
admin.add_view(GitRepoView(GitRepo, db, category="Git"))


# Kanban tables backing the /kanban page (boards + columns + tasks + the
# append-only task-event audit trail). Same low-level inspection role as the
# Cron category; the curated surface is the /kanban page itself.
def _kanban_open_link(view, context, model, name):
    """Virtual column linking to /kanban deep-linked to this row's board."""
    board_uuid = getattr(model, "board_uuid", None) or model.uuid
    return Markup(f'<a href="/kanban?board={escape(board_uuid)}">inspect ↗</a>')


def _kanban_board_label(view, context, model, name):
    """Render a board_uuid column as truncated uuid + the board's name."""
    bid = getattr(model, name)
    if not bid:
        return ""
    full = str(bid)
    short = Markup(f'<code title="{escape(full)}">{escape(full[:6])}</code>')
    board = db.session.query(KanbanBoard).filter_by(uuid=bid).first()
    return Markup(f"{short}<br>{escape(board.name)}") if board else Markup(
        f"{short}<br><i>(deleted board)</i>")


def _kanban_column_label(view, context, model, name):
    """Render a column_uuid column as truncated uuid + the column's name."""
    cid = getattr(model, name)
    if not cid:
        return ""
    full = str(cid)
    short = Markup(f'<code title="{escape(full)}">{escape(full[:6])}</code>')
    col = db.session.query(KanbanColumn).filter_by(uuid=cid).first()
    return Markup(f"{short}<br>{escape(col.name)}") if col else Markup(
        f"{short}<br><i>(deleted column)</i>")


def _kanban_task_label(view, context, model, name):
    """Render a task_uuid column as truncated uuid + the task's title."""
    tid = getattr(model, name)
    if not tid:
        return ""
    full = str(tid)
    short = Markup(f'<code title="{escape(full)}">{escape(full[:6])}</code>')
    task = db.session.query(KanbanTask).filter_by(uuid=tid).first()
    return Markup(f"{short}<br>{escape(task.title)}") if task else Markup(
        f"{short}<br><i>(deleted task)</i>")


def _kanban_agent_label(view, context, model, name):
    """Render an agent reference (KanbanTask.agent_uuid, or an event's free-text
    `actor` that may hold an agent uuid) as truncated uuid + the agent_config
    role name. Non-uuid actors (e.g. 'human') render verbatim."""
    from agents.config import agent_config

    raw = getattr(model, name)
    if not raw:
        return ""
    try:
        aid = UUID(str(raw))
    except (ValueError, TypeError):
        return escape(str(raw))  # e.g. 'human'
    full = str(aid)
    short = Markup(f'<code title="{escape(full)}">{escape(full[:6])}</code>')
    role = next((n for n, e in agent_config.items() if e["uuid"] == aid), None)
    return Markup(f"{short}<br>@{escape(role)}") if role else Markup(
        f"{short}<br><i>(unknown agent)</i>")


def _kanban_folder_label(view, context, model, name):
    """Render a kanban-folder uuid column (a folder's `parent_uuid`) as a
    truncated uuid (full on hover) on one line and the folder's name below.
    parent_uuid is a plain self-referencing column (no FK), so look it up by
    uuid."""
    fid = getattr(model, name)
    if not fid:
        return ""
    full = str(fid)
    short = Markup(f'<code title="{escape(full)}">{escape(full[:6])}</code>')
    folder = db.session.query(KanbanBoardFolder).filter_by(uuid=fid).first()
    return Markup(f"{short}<br>{escape(folder.name)}") if folder else short


def _kanban_folder_open_link(view, context, model, name):
    """Virtual column linking to /kanban deep-linked to this folder. /kanban
    takes a single ?id=<uuid> that resolves to either a folder or a board."""
    return Markup(f'<a href="/kanban?id={escape(model.uuid)}">inspect ↗</a>')


class KanbanBoardFolderView(ModelView):
    column_list = ("kanban_link", "position", "uuid", "name", "description",
                   "parent_uuid", "created_at", "updated_at")
    column_default_sort = ("position", False)
    column_labels = {"kanban_link": "Kanban page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "parent_uuid": _kanban_folder_label,
        "kanban_link": _kanban_folder_open_link,
    }


class KanbanBoardView(ModelView):
    column_list = ("kanban_link", "position", "uuid", "name", "description",
                   "created_at", "updated_at")
    column_default_sort = ("position", False)
    column_labels = {"kanban_link": "Kanban page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "kanban_link": _kanban_open_link,
    }


class KanbanColumnView(ModelView):
    column_list = ("kanban_link", "board_uuid", "position", "uuid", "name",
                   "created_at", "updated_at")
    column_default_sort = ("position", False)
    column_labels = {"kanban_link": "Kanban page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "board_uuid": _kanban_board_label,
        "kanban_link": _kanban_open_link,
    }


class KanbanTaskView(ModelView):
    column_list = ("kanban_link", "board_uuid", "column_uuid", "position",
                   "uuid", "title", "agent_uuid", "claimed_by",
                   "claim_expires_at", "description", "created_at", "updated_at")
    column_default_sort = ("position", False)
    column_labels = {"kanban_link": "Kanban page", "agent_uuid": "Assignee",
                     "claimed_by": "Claimed by"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "board_uuid": _kanban_board_label,
        "column_uuid": _kanban_column_label,
        "agent_uuid": _kanban_agent_label,
        "claimed_by": _kanban_agent_label,
        "kanban_link": _kanban_open_link,
    }


class KanbanTaskEventView(ModelView):
    column_list = ("created_at", "task_uuid", "kind", "actor", "detail")
    column_default_sort = ("id", True)  # newest first
    column_labels = {"task_uuid": "Task"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "task_uuid": _kanban_task_label,
        "actor": _kanban_agent_label,
    }


admin.add_view(KanbanBoardFolderView(KanbanBoardFolder, db, category="Kanban"))
admin.add_view(KanbanBoardView(KanbanBoard, db, category="Kanban"))
admin.add_view(KanbanColumnView(KanbanColumn, db, category="Kanban"))
admin.add_view(KanbanTaskView(KanbanTask, db, category="Kanban"))
admin.add_view(KanbanTaskEventView(KanbanTaskEvent, db, category="Kanban"))
