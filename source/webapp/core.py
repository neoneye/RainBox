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
    DecisionCall,
    HousekeepingAttempt,
    HousekeepingJob,
    RoomChunk,
    RoomTopic,
    RoomTopicMember,
    RoomTopicSuggestion,
    SecondOpinionAssessment,
    SecondOpinionReview,
    assistant_step_path,
    ChatMessage,
    Chatroom,
    ChatroomFolder,
    ChatroomMember,
    ChatUser,
    CronFolder,
    CronJob,
    CronRun,
    EvalCase,
    BenchmarkResult,
    EvalResult,
    EvalRun,
    FeedbackEvent,
    BridgeBinding,
    BridgeConnector,
    BridgeCredential,
    BridgeFolder,
    GitFolder,
    GitRepo,
    Inbox,
    Journal,
    KanbanBoard,
    KanbanBoardFolder,
    KanbanColumn,
    KanbanTask,
    KanbanTaskEvent,
    LlmCall,
    MemoryClaim,
    MemoryEmbedding,
    MemoryEvidence,
    MemoryRejectedValue,
    ModelConfig,
    ModelConfigOverride,
    ModelGroup,
    ModelGroupMember,
    Persona,
    PersonaFolder,
    PersonaRevision,
    Profile,
    ProfileFolder,
    Prompt,
    PromptFolder,
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


@app.after_request
def _single_date_header(resp):
    """Emit exactly one `Date` header.

    Flask stamps `Date` on static-file responses (`send_file`), and the WSGI
    server stamps its own afterwards — so static assets went out with the
    header twice, which RFC 9110 forbids. Chrome tolerates it; Firefox stalls
    the request, and a page whose body is rendered by an external script then
    renders as a blank page that never finishes loading (`/git`, `/cron`,
    `/kanban`, `/memory`, `/profile`).

    Dropping Flask's copy here leaves the server's, which is always added.
    This hook runs before the server writes, so it cannot remove that one —
    verified by counting the headers actually on the wire, not the ones Flask
    reports."""
    del resp.headers["Date"]
    return resp


# Every response says it came from Rainbox. On macOS the AirPlay Receiver also
# listens on port 5000 and answers anything sent while Rainbox is down (a
# restart) with an empty 403; a page tells the two apart by this header.
RAINBOX_HEADER = "X-Rainbox"


@app.after_request
def _mark_rainbox(resp):
    resp.headers[RAINBOX_HEADER] = "1"
    return resp


# Record every LLM call this process makes to `llm_call` (the /activity page).
# After init_db, because the recorder's sink writes through db.session.
from llm.activity import install_activity_recorder  # noqa: E402

install_activity_recorder()

# Shared navigation, registered as an includable template so every page can
# do {% include "_nav.html" %} and stay in sync. It is a sidebar on the left:
# a rail of icons, which widens over the page while the mouse is on it and
# shows each item's name. A page reached from the sidebar begins with it
# open. The page is not moved or squeezed by that: it
# keeps the rail's width as its left padding and nothing more. A group
# (Memory, Assistant, ...) unfolds its pages on a click, and the group of the
# page that is shown is unfolded already. The active item is derived from
# request.endpoint. Flask Admin is not in it: the Home page links to it.
# /modelgrouppriorities deliberately does NOT include this — it has its own
# "× Close" button instead. The UI is for a desktop's wide window: there is
# no narrow layout.
#
# Every item says on hover what its page is for, in a line.
#
# The icons are drawn here, in one weight and one colour (currentColor), so
# an item's icon takes the colour of its text.
NAV_TEMPLATE = """
<style>
  /* The page keeps clear of the rail, whatever padding it set for itself. */
  html body{padding-left:52px}
  .pp-nav{position:fixed;top:0;left:0;bottom:0;z-index:1000;width:52px;box-sizing:border-box;
          display:flex;flex-direction:column;gap:2px;padding:10px 8px;overflow-x:hidden;overflow-y:auto;
          border-right:1px solid #e5e7eb;background:#fff;font-family:system-ui,sans-serif;
          transition:width 0.18s ease,box-shadow 0.18s ease;scrollbar-width:none}
  .pp-nav::-webkit-scrollbar{display:none}
  /* Open: wider, and over the page. */
  .pp-nav.pp-open{width:230px;box-shadow:6px 0 24px rgba(0,0,0,0.12)}
  .pp-nav .pp-item{display:flex;align-items:center;gap:12px;height:34px;padding:0 8px;border-radius:8px;
          flex:0 0 auto;color:#6c757d;text-decoration:none;font-size:0.92rem;font-weight:500;
          white-space:nowrap;cursor:pointer;-webkit-user-select:none;user-select:none}
  .pp-nav .pp-item:hover{color:#1a1a2e;background:#f1f5f9}
  .pp-nav a.pp-item.pp-active,.pp-nav details.pp-dd.pp-active>summary{color:#1a1a2e;font-weight:600;background:#eef2ff}
  .pp-nav svg{fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;flex:0 0 auto}
  /* A name is there only while the sidebar is open: the rail shows icons. */
  .pp-nav .pp-label{flex:1 1 auto;opacity:0;transition:opacity 0.12s ease}
  .pp-nav.pp-open .pp-label{opacity:1}
  .pp-nav .pp-chev{opacity:0;transition:transform 0.15s ease,opacity 0.12s ease}
  .pp-nav.pp-open .pp-chev{opacity:0.7}
  .pp-nav details.pp-dd[open] .pp-chev{transform:rotate(90deg)}
  .pp-nav details.pp-dd{flex:0 0 auto}
  .pp-nav details.pp-dd>summary{list-style:none}
  .pp-nav details.pp-dd>summary::-webkit-details-marker{display:none}
  /* A group's pages, under it. Hidden on the rail, which has no room for them. */
  .pp-nav details.pp-dd>.pp-dd-menu{display:none;flex-direction:column;gap:1px;margin:2px 0 4px 26px;
          padding-left:10px;border-left:1px solid #e5e7eb}
  .pp-nav.pp-open details.pp-dd[open]>.pp-dd-menu{display:flex}
  .pp-nav .pp-dd-menu a{padding:5px 10px;border-radius:6px;color:#6c757d;text-decoration:none;
          font-size:0.86rem;font-weight:500;white-space:nowrap}
  .pp-nav .pp-dd-menu a:hover{color:#1a1a2e;background:#f1f5f9}
  .pp-nav .pp-dd-menu a.pp-active{color:#1a1a2e;font-weight:600}
  .pp-content{padding:1.5em 24px 2em}
  /* Open from the first frame, for a page reached from the sidebar. */
  .pp-nav.pp-instant,.pp-nav.pp-instant .pp-label,.pp-nav.pp-instant .pp-chev{transition:none}
  @media (prefers-reduced-motion: reduce){
    .pp-nav,.pp-nav .pp-label,.pp-nav .pp-chev{transition:none}
  }
</style>
<nav class="pp-nav" id="pp-nav">
  <a href="{{ url_for('index') }}" class="pp-item {{ 'pp-active' if request.endpoint == 'index' }}" title="The start page: the agents, and a few places to begin"><svg class="pp-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/></svg><span class="pp-label">Home</span></a>
  <a href="{{ url_for('chat_page') }}" class="pp-item {{ 'pp-active' if request.endpoint == 'chat_page' }}" title="Chatrooms, where you write with the assistant and the agents"><svg class="pp-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z"/></svg><span class="pp-label">Chat</span></a>
  <a href="{{ url_for('cron_page') }}" class="pp-item {{ 'pp-active' if request.endpoint == 'cron_page' }}" title="Scheduled jobs: send a message or run a command at set times"><svg class="pp-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/></svg><span class="pp-label">Cron</span></a>
  <a href="{{ url_for('kanban_page') }}" class="pp-item {{ 'pp-active' if request.endpoint == 'kanban_page' }}" title="Boards of tasks that you and the agents keep track of"><svg class="pp-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><rect x="3.5" y="5" width="4.5" height="14" rx="1"/><rect x="9.75" y="5" width="4.5" height="8" rx="1"/><rect x="16" y="5" width="4.5" height="11" rx="1"/></svg><span class="pp-label">Kanban</span></a>
  <details class="pp-dd {{ 'pp-active' if request.endpoint in ('memory_page', 'memory_developer_page') }}" {{ 'open' if request.endpoint in ('memory_page', 'memory_developer_page') }}>
    <summary class="pp-item" title="What the assistant remembers, and how it looks it up"><svg class="pp-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/></svg><span class="pp-label">Memory</span><svg class="pp-chev" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></summary>
    <div class="pp-dd-menu">
      <a href="{{ url_for('memory_page') }}" class="{{ 'pp-active' if request.endpoint == 'memory_page' }}" title="Review what the assistant remembers, and correct it">Memories</a>
      <a href="{{ url_for('memory_developer_page') }}" class="{{ 'pp-active' if request.endpoint == 'memory_developer_page' }}" title="Type a query and see what the memory returns for it">Developer</a>
    </div>
  </details>
  <details class="pp-dd {{ 'pp-active' if request.endpoint in ('assistant_page', 'assistant_overview_page', 'housekeeping_page', 'housekeeping_job_page', 'housekeeping_room_page', 'second_opinion_page', 'persona_page') }}" {{ 'open' if request.endpoint in ('assistant_page', 'assistant_overview_page', 'housekeeping_page', 'housekeeping_job_page', 'housekeeping_room_page', 'second_opinion_page', 'persona_page') }}>
    <summary class="pp-item" title="The assistant's runs, its background work, and how it is set up"><svg class="pp-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><rect x="5" y="8" width="14" height="11" rx="2"/><path d="M12 4v4"/><circle cx="12" cy="3.5" r="0.8"/><path d="M9.5 12.5v2"/><path d="M14.5 12.5v2"/><path d="M2.5 12.5v3"/><path d="M21.5 12.5v3"/></svg><span class="pp-label">Assistant</span><svg class="pp-chev" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></summary>
    <div class="pp-dd-menu">
      <a href="{{ url_for('assistant_overview_page') }}" class="{{ 'pp-active' if request.endpoint in ('assistant_page', 'assistant_overview_page') }}" title="Every run of the assistant; open one to inspect its steps">Runs</a>
      <a href="{{ url_for('housekeeping_page') }}" class="{{ 'pp-active' if request.endpoint in ('housekeeping_page', 'housekeeping_job_page', 'housekeeping_room_page') }}" title="Background work after a turn: summaries of runs and chatrooms">Housekeeping</a>
      <a href="{{ url_for('second_opinion_page') }}" class="{{ 'pp-active' if request.endpoint == 'second_opinion_page' }}" title="The checks made before an action runs, and your verdict on each">Second opinion</a>
      <a href="{{ url_for('persona_page') }}" class="{{ 'pp-active' if request.endpoint == 'persona_page' }}" title="The assistant's personas: the text that sets its character">Persona</a>
    </div>
  </details>
  <a href="{{ url_for('git_page') }}" class="pp-item {{ 'pp-active' if request.endpoint == 'git_page' }}" title="The git repositories the agents work with"><svg class="pp-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 7v10"/><path d="M18 10v1a4 4 0 0 1-4 4H6"/></svg><span class="pp-label">Git</span></a>
  <details class="pp-dd {{ 'pp-active' if request.endpoint in ('models_page', 'modelgroups_page', 'agent_models_page') }}" {{ 'open' if request.endpoint in ('models_page', 'modelgroups_page', 'agent_models_page') }}>
    <summary class="pp-item" title="The language models, their groups, and which agent uses which"><svg class="pp-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5"/><path d="M12 12v9"/></svg><span class="pp-label">Models</span><svg class="pp-chev" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></summary>
    <div class="pp-dd-menu">
      <a href="{{ url_for('models_page') }}" class="{{ 'pp-active' if request.endpoint == 'models_page' }}" title="Each configured model: its provider, name and options">Configs</a>
      <a href="{{ url_for('modelgroups_page') }}" class="{{ 'pp-active' if request.endpoint == 'modelgroups_page' }}" title="Ordered lists of models; the first that works is used">Groups</a>
      <a href="{{ url_for('agent_models_page') }}" class="{{ 'pp-active' if request.endpoint == 'agent_models_page' }}" title="Which model group each agent and each task uses">Agent models</a>
    </div>
  </details>
  <details class="pp-dd {{ 'pp-active' if request.endpoint in ('benchmark_basic_page', 'benchmark_editdocument_page', 'benchmark_kanban_page', 'benchmark_story_page', 'benchmark_vision_page') }}" {{ 'open' if request.endpoint in ('benchmark_basic_page', 'benchmark_editdocument_page', 'benchmark_kanban_page', 'benchmark_story_page', 'benchmark_vision_page') }}>
    <summary class="pp-item" title="Tests that measure how well each model does a kind of task"><svg class="pp-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M4 20h16"/><path d="M7 20v-6"/><path d="M12 20V5"/><path d="M17 20v-10"/></svg><span class="pp-label">Benchmark</span><svg class="pp-chev" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></summary>
    <div class="pp-dd-menu">
      <a href="{{ url_for('benchmark_basic_page') }}" class="{{ 'pp-active' if request.endpoint == 'benchmark_basic_page' }}" title="Short tasks with a known answer">Basic</a>
      <a href="{{ url_for('benchmark_kanban_page') }}" class="{{ 'pp-active' if request.endpoint == 'benchmark_kanban_page' }}" title="Moving tasks on a kanban board">Kanban</a>
      <a href="{{ url_for('benchmark_story_page') }}" class="{{ 'pp-active' if request.endpoint == 'benchmark_story_page' }}" title="Writing a story to a brief">Story</a>
      {# Out of alphabetical order on purpose: Edit document, then Vision, last. #}
      <a href="{{ url_for('benchmark_editdocument_page') }}" class="{{ 'pp-active' if request.endpoint == 'benchmark_editdocument_page' }}" title="Editing a document as instructed">Edit document</a>
      <a href="{{ url_for('benchmark_vision_page') }}" class="{{ 'pp-active' if request.endpoint == 'benchmark_vision_page' }}" title="Questions about images">Vision</a>
    </div>
  </details>
  <details class="pp-dd {{ 'pp-active' if request.endpoint in ('demo_tts_kokoro', 'demo_tts_lfm3', 'demo_tts_dotstts', 'demo_stt_whisper', 'demo_stt_gemma4', 'demo_stt_lfm3', 'demo_voice_echo') }}" {{ 'open' if request.endpoint in ('demo_tts_kokoro', 'demo_tts_lfm3', 'demo_tts_dotstts', 'demo_stt_whisper', 'demo_stt_gemma4', 'demo_stt_lfm3', 'demo_voice_echo') }}>
    <summary class="pp-item" title="Try text-to-speech and speech-to-text models"><svg class="pp-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/><path d="M9 21h6"/></svg><span class="pp-label">Voice</span><svg class="pp-chev" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></summary>
    <div class="pp-dd-menu">
      <a href="{{ url_for('demo_tts_kokoro') }}" class="{{ 'pp-active' if request.endpoint == 'demo_tts_kokoro' }}" title="Text to speech with Kokoro">TTS kokoro</a>
      <a href="{{ url_for('demo_tts_lfm3') }}" class="{{ 'pp-active' if request.endpoint == 'demo_tts_lfm3' }}" title="Text to speech with LFM3">TTS lfm3</a>
      <a href="{{ url_for('demo_stt_whisper') }}" class="{{ 'pp-active' if request.endpoint == 'demo_stt_whisper' }}" title="Speech to text with Whisper">STT whisper</a>
      <a href="{{ url_for('demo_stt_gemma4') }}" class="{{ 'pp-active' if request.endpoint == 'demo_stt_gemma4' }}" title="Speech to text with Gemma 4">STT gemma4</a>
      <a href="{{ url_for('demo_stt_lfm3') }}" class="{{ 'pp-active' if request.endpoint == 'demo_stt_lfm3' }}" title="Speech to text with LFM3">STT lfm3</a>
      <a href="{{ url_for('demo_voice_echo') }}" class="{{ 'pp-active' if request.endpoint == 'demo_voice_echo' }}" title="Speak, and hear it said back: speech to text, then text to speech">Echo</a>
      <a href="{{ url_for('demo_tts_dotstts') }}" class="{{ 'pp-active' if request.endpoint == 'demo_tts_dotstts' }}" title="Text to speech in a cloned voice">Clone</a>
    </div>
  </details>
  <a href="{{ url_for('find_page') }}" class="pp-item {{ 'pp-active' if request.endpoint == 'find_page' }}" title="Paste a uuid to learn what it is and jump to it"><svg class="pp-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="M20 20l-4.6-4.6"/></svg><span class="pp-label">Find</span></a>
  <a href="{{ url_for('activity_page') }}" class="pp-item {{ 'pp-active' if request.endpoint == 'activity_page' }}" title="Every model call: how long it took, its tokens, and the cache"><svg class="pp-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg><span class="pp-label">Activity</span></a>
  <a href="{{ url_for('doctor_page') }}" class="pp-item {{ 'pp-active' if request.endpoint == 'doctor_page' }}" title="Checks whether the app's environment is healthy"><svg class="pp-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M12 8v8"/><path d="M8 12h8"/></svg><span class="pp-label">Doctor</span></a>
  <a href="{{ url_for('demo_multimodal') }}" class="pp-item {{ 'pp-active' if request.endpoint == 'demo_multimodal' }}" title="Try a local model on an image or an audio file"><svg class="pp-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8.5" cy="10" r="1.5"/><path d="M21 16l-5-5-8 8"/></svg><span class="pp-label">Multimodal</span></a>
  <details class="pp-dd {{ 'pp-active' if request.endpoint in ('profile_page', 'config_page', 'bridges_page', 'prompt_page') }}" {{ 'open' if request.endpoint in ('profile_page', 'config_page', 'bridges_page', 'prompt_page') }}>
    <summary class="pp-item" title="Who the people are, how the app is configured, its outside chat services and its system prompts"><svg class="pp-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M10.2 5.6L10.5 3.1h3l.3 2.5 1.5.7 1.9-1.6 2.1 2.1-1.6 1.9.7 1.5 2.5.3v3l-2.5.3-.7 1.5 1.6 1.9-2.1 2.1-1.9-1.6-1.5.7-.3 2.5h-3l-.3-2.5-1.5-.7-1.9 1.6-2.1-2.1 1.6-1.9-.7-1.5-2.5-.3v-3l2.5-.3.7-1.5-1.6-1.9 2.1-2.1 1.9 1.6z"/><circle cx="12" cy="12" r="3"/></svg><span class="pp-label">Settings</span><svg class="pp-chev" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></summary>
    <div class="pp-dd-menu">
      <a href="{{ url_for('profile_page') }}" class="{{ 'pp-active' if request.endpoint == 'profile_page' }}" title="The record of each person: name, language, formats, contact">Profile</a>
      <a href="{{ url_for('prompt_page') }}" class="{{ 'pp-active' if request.endpoint == 'prompt_page' }}" title="System prompts for the models, kept in versions">Prompt</a>
      <a href="{{ url_for('bridges_page') }}" class="{{ 'pp-active' if request.endpoint == 'bridges_page' }}" title="Connections between chatrooms and outside chat services">Bridges</a>
      <a href="{{ url_for('config_page') }}" class="{{ 'pp-active' if request.endpoint == 'config_page' }}" title="The app's configuration, with where each value comes from">Configuration</a>
    </div>
  </details>

</nav>
<script>
  // The sidebar opens once the mouse has rested on the rail for a moment, so
  // it does not open for a mouse that only passes over it, and closes when
  // the mouse leaves. A key's focus inside it opens it too.
  (function () {
    var nav = document.getElementById('pp-nav');
    if (!nav) { return; }
    var OPEN_AFTER_MS = 150;
    var timer = null;
    function open() { nav.classList.add('pp-open'); }
    function close() {
      if (timer) { clearTimeout(timer); timer = null; }
      nav.classList.remove('pp-open');
    }
    nav.addEventListener('mouseenter', function () {
      if (timer) { clearTimeout(timer); }
      timer = setTimeout(open, OPEN_AFTER_MS);
    });
    nav.addEventListener('mouseleave', close);
    nav.addEventListener('focusin', open);
    nav.addEventListener('focusout', function (e) {
      if (!nav.contains(e.relatedTarget) && !nav.matches(':hover')) { close(); }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { close(); }
    });
    // A page reached from the sidebar begins with the sidebar open, as it
    // was when the link was clicked, and with no animation: the mouse is
    // still on it. The click leaves a note for the next page, which reads it
    // here, before the page is first drawn.
    var CAME_FROM_NAV = 'pp-nav.came-from-nav';
    nav.addEventListener('click', function (e) {
      if (!e.target.closest('a[href]')) { return; }
      try { sessionStorage.setItem(CAME_FROM_NAV, '1'); } catch (err) {}
    });
    var cameFromNav = false;
    try {
      cameFromNav = sessionStorage.getItem(CAME_FROM_NAV) === '1';
      sessionStorage.removeItem(CAME_FROM_NAV);
    } catch (err) {}
    if (cameFromNav) {
      nav.classList.add('pp-instant');
      open();
      // Animations come back once the open sidebar has been drawn.
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { nav.classList.remove('pp-instant'); });
      });
      // The browser reports no mouse position until the mouse moves. If it
      // then turns out to be off the sidebar, the sidebar closes.
      document.addEventListener('mousemove', function () {
        if (!nav.matches(':hover')) { close(); }
      }, {once: true});
    }
  })();
</script>
"""

_existing_loader = app.jinja_env.loader
app.jinja_env.loader = ChoiceLoader(
    [DictLoader({"_nav.html": NAV_TEMPLATE})]
    + ([_existing_loader] if _existing_loader is not None else [])
)

benchmark_runner = BenchmarkRunner()
kanban_benchmark_runner = BenchmarkRunner(spec_set="kanban")
story_benchmark_runner = BenchmarkRunner(spec_set="story")

from benchmarks.editdocument_runner import BenchmarkEditDocumentRunner
benchmark_editdocument_runner = BenchmarkEditDocumentRunner(app)

from benchmarks.vision_runner import VisionBenchmarkRunner
vision_benchmark_runner = VisionBenchmarkRunner()


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
        systemone_by_name=(
            {m["id"]: ("decision" in (m.get("capabilities") or []))
             for m in native if m.get("id")}
            if native is not None else None),
        force_update_arguments=force_update_arguments,
        # A curated provider's catalog is far too large to mirror into rows;
        # the operator adds them one at a time on /model, and sync only keeps
        # `available` honest for the ones already there.
        create_missing=not prov.curated,
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
    # Editing arrives later via a dedicated /config page.
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
    """A debug-assistant row may store a {run_uuid, step_index, summary} pointer.
    Resolve it to the step's full action/reason/args/observation (no truncation),
    or None if `model` is not a resolvable debug-assistant row."""
    if model.kind != "debug-assistant":
        return None
    try:
        ptr = json.loads(model.text or "")
        run_uuid, step_index = ptr.get("run_uuid"), ptr.get("step_index")
    except (ValueError, TypeError):
        return None
    if run_uuid is None:
        return None
    steps = (db.session.query(AssistantStep)
             .filter_by(run_uuid=run_uuid, step_index=step_index)
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
    # `meta` is a reserved attribute on wtforms.Form (it holds the FormMeta
    # instance). Scaffolding the column of the same name shadows it, and
    # Form.process() then calls JSONField.wrap_formdata and dies — which took
    # out the whole edit page, not just this field. Keep the column out of the
    # form and surface it through the read-only extra below.
    form_excluded_columns = ("meta",)
    # Flask-Admin's form converter skips the UUID columns, so the edit form shows
    # neither the room/sender uuids nor their names. Add read-only reference
    # fields that show both ("name (uuid)"), filled in on_form_prefill.
    form_extra_fields = {
        "room": StringField("Room", render_kw={"readonly": True}),
        "sender": StringField("Sender", render_kw={"readonly": True}),
        "meta_json": TextAreaField(
            "Meta (read-only)",
            render_kw={"readonly": True, "rows": 6, "style": "width:100%"}),
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
        form.meta_json.data = json.dumps(msg.meta, indent=2) if msg.meta else ""
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
    # Flask-Admin hides the primary key by default; show it so the journal_id is
    # visible (truncated by the formatter below).
    column_display_pk = True
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


class MemoryRejectedValueView(ModelView):
    column_list = (
        "uuid", "scope", "subj_pred_key", "value_key", "claim_text",
        "hit_count", "last_hit_at", "created_at",
    )
    # Newest tombstones first — operators reviewing suppressions want the
    # most recently rejected values (and the most-recently-hit) on top.
    column_default_sort = ("created_at", True)


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
admin.add_view(MemoryRejectedValueView(MemoryRejectedValue, db, category="Memory"))
admin.add_view(FeedbackEventView(FeedbackEvent, db, category="Feedback"))
admin.add_view(RetrievalEventView(RetrievalEvent, db, category="Telemetry"))
admin.add_view(EvalCaseView(EvalCase, db, category="Feedback"))
# Model-config tables (the Models pages are the curated UI; these expose the raw rows).
admin.add_view(ModelView(ModelGroup, db, category="Config"))
admin.add_view(ModelView(ModelGroupMember, db, category="Config"))
admin.add_view(ModelView(AgentModelBinding, db, category="Config"))
# Assistant ReAct loop: runs, per-step trace, control channel, write intents.
class AssistantRunView(ModelView):
    # Newest first — the integer id is internal plumbing, not the sort the
    # operator cares about.
    column_default_sort = ("started_at", True)


def _format_step_trace_link(view, context, model, name):
    """Render a step's uuid cell as a link to its /assistant trace location —
    the run, scrolled to this step's anchor (id="step-<uuid>"). Shows only the
    first 6 chars (full value on hover), to match the other uuid columns."""
    full = str(model.uuid)
    href = assistant_step_path(model.run_uuid, model.uuid)
    return Markup(f'<a href="{escape(href)}" title="{escape(full)}">'
                  f'<code>{escape(full[:6])}</code> ↗</a>')


class AssistantStepView(ModelView):
    # Newest first; the uuid links to the run's trace, scrolled to this step.
    # uuids are shown short (first 6 chars, full on hover) so columns stay narrow,
    # and datetimes drop sub-seconds (requested_at / created_at / settled_at).
    column_default_sort = ("id", True)
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _format_step_trace_link,
        "run_uuid": _fmt_short_uuid,
        "model_uuid": _fmt_short_uuid,
        "model_group_uuid": _fmt_short_uuid,
    }


admin.add_view(AssistantRunView(AssistantRun, db, category="Assistant"))
admin.add_view(AssistantStepView(AssistantStep, db, category="Assistant"))
admin.add_view(ModelView(AssistantControl, db, category="Assistant"))
admin.add_view(ModelView(AssistantWriteIntent, db, category="Assistant"))


def _format_review_trace_link(view, context, model, name):
    """Render a review's uuid cell as a link to the trace it gated — the run,
    scrolled to the gated step when there is one. Same affordance and width as
    the step list's uuid cell. `step_uuid` is nullable (the review runs before
    dispatch), so fall back to the run without an anchor."""
    full = str(model.uuid)
    href = (assistant_step_path(model.run_uuid, model.step_uuid)
            if model.step_uuid else f"/assistant?id={model.run_uuid}")
    return Markup(f'<a href="{escape(href)}" title="{escape(full)}">'
                  f'<code>{escape(full[:6])}</code> ↗</a>')


def _format_review_problems(view, context, model, name):
    """The findings as one plain line each — the category, then the sentence.
    The stored shape is {category, text}, whose default repr is both unreadable
    and wider than the finding itself. The category is kept per line because the
    `categories` column only carries the row's distinct set, losing which
    finding rests on which ground when a review reports several."""
    # Local, like the other agents.* imports here: core.py loads early and
    # agents.assistant is heavy.
    from agents.assistant import problem_texts

    rows = []
    for text, problem in zip(problem_texts(model.problems), model.problems or []):
        category = (problem or {}).get("category") if isinstance(problem, dict) else None
        prefix = f"{escape(str(category))} — " if category else ""
        rows.append(f"{prefix}{escape(text)}")
    return Markup("<br>".join(rows)) if rows else ""


class SecondOpinionReviewView(ModelView):
    # Newest first; the review is a record of what a model said at a point in
    # time, so it is read-only here — the operator's judgment of it goes in
    # second_opinion_assessment, not by editing the row.
    column_default_sort = ("id", True)
    can_edit = False
    can_create = False
    column_type_formatters = CRON_TYPE_FORMATTERS
    # Every uuid column is shortened to 6 chars (full value on hover), as in
    # AssistantStepView — a review row carries five of them, and at full width
    # they push verdict/categories/problems off screen. `run_uuid`/`step_uuid`
    # are foreign keys, which Flask-Admin leaves out of the list, so they are
    # reachable through the uuid cell's link rather than as columns.
    column_formatters = {
        "uuid": _format_review_trace_link,
        "journal_id": _fmt_short_uuid,
        "room_uuid": _fmt_short_uuid,
        "agent_uuid": _fmt_short_uuid,
        "model_uuid": _fmt_short_uuid,
        "problems": _format_review_problems,
    }
    # The prompts and the reviewer's raw output are long; keep the list scannable
    # (they stay on the detail view).
    column_exclude_list = ("system_prompt", "user_prompt", "reasoning", "response")


class SecondOpinionAssessmentView(ModelView):
    # Append-only: a changed mind is a new row, so editing is off.
    column_default_sort = ("id", True)
    can_edit = False
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {"uuid": _fmt_short_uuid}


admin.add_view(SecondOpinionReviewView(
    SecondOpinionReview, db, category="Assistant"))
admin.add_view(SecondOpinionAssessmentView(
    SecondOpinionAssessment, db, category="Assistant"))


class HousekeepingJobView(ModelView):
    # The record of what ran after a turn. Read here; the Housekeeping page
    # is where a job is enqueued again.
    column_default_sort = ("created_at", True)
    can_create = False
    can_edit = False
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {"uuid": _fmt_short_uuid}


class HousekeepingAttemptView(ModelView):
    column_default_sort = ("started_at", True)
    can_create = False
    can_edit = False
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {"uuid": _fmt_short_uuid}


admin.add_view(HousekeepingJobView(HousekeepingJob, db, category="Assistant"))
admin.add_view(HousekeepingAttemptView(
    HousekeepingAttempt, db, category="Assistant"))


class RoomChunkView(ModelView):
    # Written once by the housekeeping role; read-only here.
    column_default_sort = ("created_at", True)
    can_create = False
    can_edit = False
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {"uuid": _fmt_short_uuid}


admin.add_view(RoomChunkView(RoomChunk, db, category="Assistant"))


class RoomTopicView(ModelView):
    # Written by the housekeeping role, and by the operator on the room's
    # chunks page, which is where a name or an explanation is edited.
    column_default_sort = ("created_at", True)
    can_create = False
    can_edit = False
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {"uuid": _fmt_short_uuid}


class RoomTopicMemberView(ModelView):
    # Written once by the housekeeping role; read-only here. The vector is
    # 768 numbers, which a list has no room for.
    column_default_sort = ("created_at", True)
    can_create = False
    can_edit = False
    column_exclude_list = ("embedding",)
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {"uuid": _fmt_short_uuid}


admin.add_view(RoomTopicView(RoomTopic, db, category="Assistant"))
admin.add_view(RoomTopicMemberView(RoomTopicMember, db, category="Assistant"))
# Two topics that look like one: answered on the room's chunks page.
admin.add_view(RoomTopicView(
    RoomTopicSuggestion, db, category="Assistant", name="Room Topic Suggestion",
    endpoint="roomtopicsuggestion"))


class DecisionCallView(ModelView):
    # One row per request to a decision model; read-only here.
    column_default_sort = ("created_at", True)
    can_create = False
    can_edit = False
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {"uuid": _fmt_short_uuid}


admin.add_view(DecisionCallView(DecisionCall, db, category="Assistant"))


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


class BenchmarkResultView(ModelView):
    """Stored benchmark cell results, behind the three /benchmark_* pages.

    Read-only: rows are written by BenchmarkRunner and pruned to their per-cell
    retention on every write, so an edit here would be silently discarded the
    next time that cell runs. Newest first, which is the order the pages read
    them in.
    """

    column_list = (
        "ended_at", "spec_set", "benchmark_name", "model_name", "target_label",
        "completed", "status", "trials_done", "trials_total",
        "correct", "mistakes", "failures", "total_elapsed",
    )
    column_default_sort = ("ended_at", True)
    column_filters = ("spec_set", "benchmark_name", "completed", "status")
    can_create = False
    can_edit = False


admin.add_view(BenchmarkResultView(BenchmarkResult, db, category="Feedback"))


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


# --- chat bridges (connectors, folders, bindings; edited on /bridges) ---------------


def _bridges_open_link(view, context, model, name):
    """Deep link into the /bridges page for this row."""
    uid = getattr(model, "uuid", None)
    if not uid:
        return ""
    return Markup(f'<a href="/bridges?id={escape(str(uid))}" target="_blank">Open</a>')


def _bridge_connector_label(view, context, model, name):
    cid = getattr(model, name)
    if not cid:
        return ""
    full = str(cid)
    short = Markup(f'<code title="{escape(full)}">{escape(full[:6])}</code>')
    conn = db.session.query(BridgeConnector).filter_by(uuid=cid).first()
    return Markup(f"{short}<br>{escape(conn.name)}") if conn else short


class BridgeConnectorView(ModelView):
    """Read-only: the credential is never here (its variable name is the
    platform's constant); every write belongs on /bridges, whose API validates
    addresses and policies, refuses deletes that would orphan rows, rewrites
    the restart nonce, notifies the running bridge, and pushes the launcher.
    An admin write would bypass all of that."""
    can_create = can_edit = can_delete = False
    column_list = (
        "bridges_link", "position", "uuid", "name", "platform", "launch_mode",
        "enabled", "base_url", "identity", "policy", "restart_nonce", "created_at", "updated_at",
    )
    column_default_sort = ("position", False)
    column_labels = {"bridges_link": "Bridges page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "restart_nonce": _fmt_short_uuid,
        "bridges_link": _bridges_open_link,
    }


class BridgeFolderView(ModelView):
    can_create = can_edit = can_delete = False   # writes go through /bridges (see BridgeConnectorView)
    column_list = (
        "bridges_link", "position", "uuid", "name", "connector_uuid", "parent_uuid",
        "enabled", "policy", "created_at", "updated_at",
    )
    column_default_sort = ("position", False)
    column_labels = {"bridges_link": "Bridges page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "parent_uuid": _fmt_short_uuid,
        "connector_uuid": _bridge_connector_label,
        "bridges_link": _bridges_open_link,
    }


class BridgeBindingView(ModelView):
    can_create = can_edit = can_delete = False   # writes go through /bridges (see BridgeConnectorView)
    column_list = (
        "bridges_link", "position", "uuid", "connector_uuid", "folder_uuid", "room_uuid",
        "address_key", "address", "enabled", "policy", "created_at", "updated_at",
    )
    column_default_sort = ("position", False)
    column_labels = {"bridges_link": "Bridges page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "folder_uuid": _fmt_short_uuid,
        "room_uuid": _fmt_short_uuid,
        "connector_uuid": _bridge_connector_label,
        "bridges_link": _bridges_open_link,
    }


class BridgeCredentialView(ModelView):
    """Read-only, and only the row's metadata: the sealed bytes are not
    listed (they are ciphertext, but nothing in the admin needs them), and
    the value itself exists nowhere the admin could show."""
    can_create = can_edit = can_delete = False
    column_list = ("connector_uuid", "version", "updated_at")
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {"connector_uuid": _bridge_connector_label}


admin.add_view(BridgeConnectorView(BridgeConnector, db, category="Bridges"))
admin.add_view(BridgeCredentialView(BridgeCredential, db, category="Bridges"))
admin.add_view(BridgeFolderView(BridgeFolder, db, category="Bridges"))
admin.add_view(BridgeBindingView(BridgeBinding, db, category="Bridges"))


# System prompt tables backing the /prompt page (folder tree + versioned
# prompts, where each row is one version linked to the one it was cloned from).
def _prompt_open_link(view, context, model, name):
    """Virtual column linking to the /prompt page deep-linked to this node, so
    an operator can jump from the admin row straight to the folder/prompt there."""
    return Markup(f'<a href="/prompt?id={escape(model.uuid)}">inspect ↗</a>')


def _prompt_folder_label(view, context, model, name):
    """Render a folder-uuid column (a prompt's `folder_uuid` or a folder's
    `parent_uuid`) as a truncated uuid (full on hover) with the folder's name
    below. Plain columns (no FK), so look the folder up by uuid."""
    fid = getattr(model, name)
    if not fid:
        return ""
    full = str(fid)
    short = Markup(f'<code title="{escape(full)}">{escape(full[:6])}</code>')
    folder = db.session.query(PromptFolder).filter_by(uuid=fid).first()
    return Markup(f"{short}<br>{escape(folder.name)}") if folder else short


class PromptFolderView(ModelView):
    column_list = (
        "prompt_link", "position", "uuid", "name", "description", "parent_uuid",
        "created_at", "updated_at",
    )
    column_default_sort = ("position", False)
    column_labels = {"prompt_link": "Prompt page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "parent_uuid": _prompt_folder_label,
        "prompt_link": _prompt_open_link,
    }


class PromptView(ModelView):
    column_list = (
        "prompt_link", "position", "uuid", "name", "parent_uuid", "folder_uuid",
        "created_at", "updated_at",
    )
    column_default_sort = ("position", False)
    column_labels = {"prompt_link": "Prompt page",
                     "parent_uuid": "Based on (version)"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "parent_uuid": _fmt_short_uuid,
        "folder_uuid": _prompt_folder_label,
        "prompt_link": _prompt_open_link,
    }


admin.add_view(PromptFolderView(PromptFolder, db, category="Prompt"))
admin.add_view(PromptView(Prompt, db, category="Prompt"))


# Person-profile tables backing the /profile page (folder tree + profiles,
# where each profile's person fields live in the sparse `data` JSONB).
def _profile_open_link(view, context, model, name):
    """Virtual column linking to the /profile page deep-linked to this node."""
    return Markup(f'<a href="/profile?id={escape(model.uuid)}">inspect ↗</a>')


def _profile_folder_label(view, context, model, name):
    """Render a folder-uuid column (a profile's `folder_uuid` or a folder's
    `parent_uuid`) as a truncated uuid (full on hover) with the folder's name
    below. Plain columns (no FK), so look the folder up by uuid."""
    fid = getattr(model, name)
    if not fid:
        return ""
    full = str(fid)
    short = Markup(f'<code title="{escape(full)}">{escape(full[:6])}</code>')
    folder = db.session.query(ProfileFolder).filter_by(uuid=fid).first()
    return Markup(f"{short}<br>{escape(folder.name)}") if folder else short


class ProfileFolderView(ModelView):
    column_list = (
        "profile_link", "position", "uuid", "name", "description", "parent_uuid",
        "created_at", "updated_at",
    )
    column_default_sort = ("position", False)
    column_labels = {"profile_link": "Profile page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "parent_uuid": _profile_folder_label,
        "profile_link": _profile_open_link,
    }


class ProfileView(ModelView):
    column_list = (
        "profile_link", "position", "uuid", "name", "folder_uuid", "data",
        "created_at", "updated_at",
    )
    column_default_sort = ("position", False)
    column_labels = {"profile_link": "Profile page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "folder_uuid": _profile_folder_label,
        "profile_link": _profile_open_link,
    }


admin.add_view(ProfileFolderView(ProfileFolder, db, category="Profile"))
admin.add_view(ProfileView(Profile, db, category="Profile"))


# Persona tables backing the /persona page (folder tree + the
# free-text persona body + its append-only revision history).
def _persona_open_link(view, context, model, name):
    """Virtual column linking to the /persona page deep-linked to this
    node (folder or persona — both select via ?id= in the tree)."""
    return Markup(f'<a href="/persona?id={escape(model.uuid)}">inspect ↗</a>')


def _persona_folder_label(view, context, model, name):
    """Render a folder-uuid column (a persona's `folder_uuid` or a
    folder's `parent_uuid`) as a truncated uuid (full on hover) with the
    folder's name below. Plain columns (no FK), so look the folder up by uuid."""
    fid = getattr(model, name)
    if not fid:
        return ""
    full = str(fid)
    short = Markup(f'<code title="{escape(full)}">{escape(full[:6])}</code>')
    folder = db.session.query(PersonaFolder).filter_by(uuid=fid).first()
    return Markup(f"{short}<br>{escape(folder.name)}") if folder else short


class PersonaFolderView(ModelView):
    column_list = (
        "persona_link", "position", "uuid", "name", "description",
        "parent_uuid", "created_at", "updated_at",
    )
    column_default_sort = ("position", False)
    column_labels = {"persona_link": "Persona page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "parent_uuid": _persona_folder_label,
        "persona_link": _persona_open_link,
    }


class PersonaView(ModelView):
    column_list = (
        "persona_link", "position", "uuid", "name", "folder_uuid",
        "created_at", "updated_at",
    )
    column_default_sort = ("position", False)
    column_labels = {"persona_link": "Persona page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    # Exclude `content` from the row form — editing it here would write the
    # text with no revision appended, breaking the invariant (see
    # PersonaRevisionView above) that the newest revision always mirrors
    # the persona's current content. Edit content through /persona.
    form_columns = ("name", "folder_uuid", "position")
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "folder_uuid": _persona_folder_label,
        "persona_link": _persona_open_link,
    }


def _persona_revision_open_link(view, context, model, name):
    """A revision has no page of its own — link to the persona whose
    history holds it."""
    return Markup(
        f'<a href="/persona?id={escape(model.persona_uuid)}">inspect ↗</a>')


class PersonaRevisionView(ModelView):
    """Read-only: revisions are append-only by design — the newest one always
    mirrors the persona's current `content`, and editing or deleting a
    revision by hand here would break that invariant."""
    can_create = False
    can_edit = False
    can_delete = False
    column_list = ("persona_link", "uuid", "persona_uuid", "created_at")
    column_default_sort = ("id", True)
    column_labels = {"persona_link": "Persona page"}
    column_type_formatters = CRON_TYPE_FORMATTERS
    column_formatters = {
        "uuid": _fmt_short_uuid,
        "persona_uuid": _fmt_short_uuid,
        "persona_link": _persona_revision_open_link,
    }


admin.add_view(PersonaFolderView(PersonaFolder, db, category="Persona"))
admin.add_view(PersonaView(Persona, db, category="Persona"))
admin.add_view(PersonaRevisionView(PersonaRevision, db, category="Persona"))


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


class LlmCallView(ModelView):
    """Observational telemetry — machine-written, so read-only in the admin.

    Newest first: an operator opening this table is almost always checking
    what the last handful of calls recorded, not paging through history.
    `prefix_chain` is excluded — a list of block hashes is unreadable in a
    grid and says nothing a human can act on.
    """

    can_create = False
    can_edit = False
    column_exclude_list = ["prefix_chain"]
    column_list = (
        "started_at",
        "provider",
        "model",
        "caller",
        "origin",
        "ok",
        "error_category",
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens_reported",
        "cached_tokens_estimated",
        "reusable_prefix_tokens",
        "prefill_ms",
        "decode_ms",
        "total_ms",
    )
    column_default_sort = ("started_at", True)


admin.add_view(LlmCallView(LlmCall, db, category="Activity"))
