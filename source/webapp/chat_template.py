"""Inline HTML/CSS/JS template for the /chat page.

Split out of webapp/chat_views.py to keep that module to just the route.
Rendered (Jinja) via render_template_string in chat_views.chat_page; kept as a
Python string constant to match the rest of webapp/ (no templates/ or static/
dir) and to preserve the no-store fast-iteration workflow.
"""

# The /chat page is a thin shell: rooms and messages are loaded from the JSON
# API in webapp/chat_api.py, and new messages are pushed live over SSE
# (/chat/stream). Nothing is rendered server-side here except the layout.
CHAT_TEMPLATE: str = """
<!doctype html>
<title>Chat &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  .chat-split{display:grid;grid-template-columns:260px 1fr;grid-template-rows:1fr;flex:1 1 auto;min-height:0}
  .chat-split.sidebar-open{grid-template-columns:260px 1fr 240px}

  .rooms{overflow:auto;min-height:0;border-right:1px solid #ddd;background:#fbfbfb;padding:0.5em}
  .rooms-head{display:flex;align-items:center;justify-content:space-between;padding:0.2em 0.4em 0.5em}
  .rooms-head .title{font-weight:600;color:#333;font-size:0.9rem}
  .new-room-btn{border:none;background:#2563eb;color:#fff;border-radius:6px;padding:0.25em 0.7em;cursor:pointer;font:inherit;font-size:0.8rem}
  .new-room-btn:hover{background:#1d4ed8}
  .rooms .note{margin:0.2em 0.4em 0.6em;color:#888;font-size:0.78rem}

  .new-room{padding:0.5em;margin:0 0.3em 0.6em;border:1px solid #e5e7eb;border-radius:8px;background:#fff}
  .new-room.hidden{display:none}
  .new-room input[type=text]{width:100%;box-sizing:border-box;padding:0.4em;border:1px solid #ccc;border-radius:6px;font:inherit}
  .new-room .agents{display:flex;flex-direction:column;gap:0.25em;margin:0.6em 0;max-height:30vh;overflow:auto}
  .new-room .agents .lbl{color:#888;font-size:0.75rem;margin-bottom:0.1em}
  .new-room .agents label{font-size:0.85rem;color:#333;display:flex;align-items:center;gap:0.4em}
  .new-room .actions{display:flex;justify-content:flex-end}
  .new-room button[type=submit]{border:none;background:#2563eb;color:#fff;border-radius:6px;padding:0.35em 0.9em;cursor:pointer;font:inherit}
  .new-room button[type=submit]:hover{background:#1d4ed8}

  .room{position:relative;display:block;width:100%;text-align:left;border:none;background:none;cursor:pointer;
        padding:0.5em 0.7em;border-radius:6px;font:inherit;color:#333}
  .room:hover{background:#eef0f6}
  .room.active{background:#e3ebfb}
  .room-name{display:block;font-weight:600}
  .room-sub{display:block;font-size:0.78rem;color:#888}
  .unread{position:absolute;right:0.5em;top:50%;transform:translateY(-50%);background:#ef4444;color:#fff;
          border-radius:999px;font-size:0.7rem;min-width:1.4em;height:1.4em;display:inline-flex;
          align-items:center;justify-content:center;padding:0 0.35em}
  /* Overflow (...) menu — only the selected room shows the kebab button. */
  .room-row{position:relative}
  /* No transform here: a transformed ancestor would become the containing block
     for the position:fixed menu, breaking its viewport anchoring. Center the
     kebab with flex instead. */
  .room-actions{position:absolute;right:0.35em;top:0;bottom:0;display:none;align-items:center}
  .room-row.active .room-actions{display:flex}
  .room-kebab{border:none;background:none;cursor:pointer;color:#6b7280;line-height:1;
              width:1.9rem;height:1.9rem;padding:0;border-radius:6px;
              display:inline-flex;align-items:center;justify-content:center}
  /* Draw the three dots in CSS rather than using the ⋯ glyph: a font glyph's ink
     sits low in the em box (system-ui rides the math axis), so flex-centering the
     line box still leaves it below mid-y. A pseudo-element dot is centered exactly. */
  .room-kebab::before{content:"";width:3px;height:3px;border-radius:50%;background:currentColor;
                      box-shadow:-5px 0 0 currentColor,5px 0 0 currentColor}
  .room-kebab:hover{background:#d2ddf6;color:#1a1a2e}
  /* position:fixed (coordinates set in JS from the kebab's rect) so the menu
     overlays the room-main column and other rows instead of being painted under
     them — a descendant of the rooms grid column can't win that stacking fight. */
  .room-menu{position:fixed;z-index:1000;min-width:150px;background:#fff;
             border:1px solid #d1d5db;border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,0.14);
             padding:0.25em;display:flex;flex-direction:column}
  .room-menu[hidden]{display:none}
  .room-menu .item{text-align:left;border:none;background:none;cursor:pointer;font:inherit;font-size:0.85rem;
                   color:#333;padding:0.45em 0.6em;border-radius:6px}
  .room-menu .item:hover{background:#eef0f6}
  .room-menu .item.danger{color:#b91c1c}

  /* ---- folder tree (ported from /cron) ---- */
  #rooms ul{list-style:none;margin:0;padding:0}
  #rooms ul ul{margin-left:0.85em;border-left:1px solid #e5e7eb;padding-left:0.35em}
  .chat-node{position:relative;display:flex;align-items:center;gap:0.4em;width:100%;
             padding:0.4em 0.6em;border-radius:6px;cursor:pointer;color:#333;font-size:0.9rem}
  .chat-node:hover{background:#eef0f6}
  .chat-node.sel{background:#e3ebfb}
  .chat-ficon{display:inline-flex;width:1.05em;height:1.05em;color:#6b7280;flex:0 0 auto}
  .chat-ficon svg{width:100%;height:100%}
  .chat-folder-label{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
  /* drag feedback (ported from cron) */
  .chat-dragging{opacity:0.4}
  .chat-drop-target{outline:2px solid #2563eb;outline-offset:-2px}
  .chat-drop-before{box-shadow:inset 0 2px 0 #2563eb}
  .chat-drop-after{box-shadow:inset 0 -2px 0 #2563eb}
  .chat-root-drop{margin:0.4em 0.3em 0;padding:0.4em;border:1px dashed #cbd5e1;border-radius:6px;
                  color:#94a3b8;font-size:0.78rem;text-align:center;display:none}
  .rooms.dragging-on .chat-root-drop{display:block}
  .chat-root-drop.over{border-color:#2563eb;color:#2563eb;background:#eff6ff}
  .new-folder-btn{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;
                  padding:0.25em 0.6em;cursor:pointer;font:inherit;font-size:0.78rem;margin-left:0.4em}
  .new-folder-btn:hover{border-color:#2563eb;color:#2563eb}
  /* modal (folder create + delete-confirm) */
  .chat-modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.35);z-index:1500}
  .chat-modal-backdrop[hidden]{display:none}
  .chat-modal{position:fixed;z-index:1600;left:50%;top:50%;transform:translate(-50%,-50%);
              background:#fff;border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,0.25);
              padding:1.2em 1.3em;width:min(420px,92vw)}
  .chat-modal[hidden]{display:none}
  .chat-modal h3{margin:0 0 0.6em;font-size:1.05rem}
  .chat-modal p{margin:0 0 0.8em;color:#444;font-size:0.9rem;line-height:1.45}
  .chat-modal input[type=text]{width:100%;box-sizing:border-box;padding:0.5em;border:1px solid #ccc;
                               border-radius:6px;font:inherit}
  .chat-modal .modal-actions{display:flex;justify-content:flex-end;gap:0.5em;margin-top:1em}
  .chat-modal button{border:none;border-radius:6px;padding:0.45em 1em;cursor:pointer;font:inherit}
  .chat-modal .btn-cancel{background:#e5e7eb;color:#374151}
  .chat-modal .btn-primary{background:#2563eb;color:#fff}
  .chat-modal .btn-danger{background:#dc2626;color:#fff}
  .chat-modal button:disabled{opacity:0.5;cursor:default}

  .room-main{display:flex;flex-direction:column;overflow:hidden;min-height:0}
  .room-title{padding:0.6em 1em;border-bottom:1px solid #eee;font-weight:600;display:flex;align-items:center;gap:0.6em}
  .room-title input#room-title-name{flex:1 1 auto;font:inherit;font-size:1.05em;font-weight:600;
        border:1px solid transparent;border-radius:6px;padding:0.2em 0.4em;background:transparent;min-width:0}
  .room-title input#room-title-name:hover{border-color:#ddd}
  .room-title input#room-title-name:focus{border-color:#2563eb;background:#fff;outline:none}
  .sidebar-mode{font:inherit;font-size:0.8rem;color:#6c757d;border:1px solid #ccc;border-radius:6px;padding:0.2em 0.4em;background:#fff;cursor:pointer}

  .room-sidebar{display:none;overflow:auto;min-height:0;border-left:1px solid #ddd;background:#fbfbfb;padding:0.8em 1em}
  .chat-split.sidebar-open .room-sidebar{display:block}
  .room-sidebar .sidebar-title{margin:0 0 0.7em;font-size:0.95rem;color:#333}
  .room-sidebar .member-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:0.45em}
  .room-sidebar .member-list li{display:flex;align-items:center;gap:0.5em;font-size:0.9rem;color:#333}
  .room-sidebar .member-name{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .room-sidebar .member-list li label.member-toggle{display:flex;align-items:center;gap:0.5em;flex:1 1 auto;cursor:pointer;margin:0}
  .room-sidebar .stat{display:flex;justify-content:space-between;padding:0.35em 0;font-size:0.9rem;border-bottom:1px solid #eee}
  .rename-room{font-size:0.78rem;font-weight:500;color:#6c757d;background:none;border:1px solid #ccc;border-radius:6px;padding:0.2em 0.7em;cursor:pointer}
  .rename-room:hover{color:#1a1a2e;border-color:#1a1a2e}

  .chat-log{flex:1 1 auto;overflow:auto;padding:1em;display:flex;flex-direction:column;gap:1.1em}
  .msg-head{font-size:0.85rem;margin-bottom:0.15em}
  .msg-sender{font-weight:600;color:#1a1a2e}
  .msg-type{font-size:0.66rem;text-transform:uppercase;letter-spacing:0.03em;padding:1px 6px;border-radius:999px;margin-left:0.5em;vertical-align:middle}
  .msg-type-human{background:#dbeafe;color:#1e40af}
  .msg-type-agent{background:#e9d5ff;color:#6b21a8}
  .msg-time{color:#888;margin-left:0.5em}
  /* Non-"message" rows (debug-router, thinking, …): a muted, dashed bubble with
     a kind badge, so they read as diagnostics rather than real chat. */
  .msg-debug{opacity:0.8;background:#f6f4fb;border:1px dashed #c4b5e0;border-radius:8px;padding:6px 10px}
  .msg-debug .msg-text{font-size:0.85rem;color:#555}
  .msg-kind{font-size:0.66rem;text-transform:uppercase;letter-spacing:0.03em;padding:1px 6px;border-radius:999px;margin-left:0.5em;vertical-align:middle;background:#fde68a;color:#92400e}
  .msg-text{line-height:1.5}
  /* Collapsible reasoning ("thinking") rows: a small link-style toggle. */
  .thinking-toggle{margin:2px 0 0;padding:0;background:none;border:none;cursor:pointer;font:inherit;
                   font-size:0.8rem;color:#6d28d9;text-decoration:underline;text-underline-offset:2px}
  .thinking-toggle:hover{color:#4c1d95}
  /* Live-streaming row: a blinking cursor after the text while tokens arrive. */
  .msg-streaming .msg-text::after{content:'▍';margin-left:1px;color:#7c6fb0;animation:pp-blink 1s steps(1) infinite}
  @keyframes pp-blink{50%{opacity:0}}
  .msg-text > :first-child{margin-top:0}
  .msg-text > :last-child{margin-bottom:0}
  .msg-text p{margin:0.4em 0}
  .msg-text ul,.msg-text ol{margin:0.4em 0;padding-left:1.4em}
  .msg-text blockquote{margin:0.4em 0;padding-left:0.8em;border-left:3px solid #ddd;color:#555}
  .msg-text code{background:#eee;padding:1px 4px;border-radius:3px;font-family:ui-monospace,monospace;font-size:90%}
  .msg-text pre{background:#f4f4f4;padding:0.6em;border-radius:5px;overflow:auto}
  .msg-text pre code{background:none;padding:0}
  .msg-text a{color:#0653a8}
  /* Single container that holds the copy button + feedback row. Owns the
     top gap between the message body and the action buttons so every
     child button starts at the same Y. The lucide SVGs inside the
     buttons size via `width="1em"`, inheriting font-size. */
  .msg-actions{display:flex;gap:0.15em;align-items:center;margin-top:calc(0.3em + 2px)}
  .copy-btn{font-size:1rem;color:#6c757d;background:none;border:1px solid transparent;border-radius:4px;padding:5px;cursor:pointer;line-height:1.4;display:inline-flex;align-items:center}
  .copy-btn:hover{color:#1a1a2e;border-color:#cbd5e1}
  .fb-row{display:inline-flex;gap:0.15em}
  .fb-btn{font-size:1rem;color:#6c757d;background:none;border:1px solid transparent;border-radius:4px;padding:5px;cursor:pointer;line-height:1.4;display:inline-flex;align-items:center}
  .fb-btn:hover{color:#1a1a2e;border-color:#cbd5e1}
  .fb-btn.fb-selected-up{color:#15803d;border-color:#15803d}
  .fb-btn.fb-selected-down{color:#b91c1c;border-color:#b91c1c}
  .fb-btn:disabled{opacity:0.6;cursor:default}
  .copy-btn svg, .fb-btn svg{display:block}

  .compose{display:flex;gap:0.5em;align-items:flex-end;padding:0.75em 1em;background:#fff;border-top:1px solid #e5e7eb}
  /* Auto-grows with content (see autoGrow): one line by default, up to 10 rows
     (line-height 1.4em + 0.5em*2 padding + 2px border, border-box), then scrolls. */
  .compose textarea{flex:1 1 auto;box-sizing:border-box;padding:0.5em;font-family:inherit;font-size:1rem;line-height:1.4;
                    border:1px solid #ccc;border-radius:6px;resize:none;overflow-y:auto;
                    min-height:calc(1.4em + 1em + 2px);max-height:calc(14em + 1em + 2px)}
  .compose button{padding:0.5em 1.2em;font-size:1rem;border:none;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer}
  .compose button:hover{background:#1d4ed8}
  .compose button:disabled{background:#9db4e8;cursor:default}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="chat-split">
  <div class="rooms">
    <div class="rooms-head">
      <span class="title">Rooms</span>
      <span>
        <button class="new-folder-btn" id="new-folder-btn" type="button">+ Folder</button>
        <button class="new-room-btn" id="new-room-btn" type="button">+ New room</button>
      </span>
    </div>
    <form class="new-room hidden" id="new-room">
      <input type="text" id="room-name" placeholder="Room name" autocomplete="off" required>
      <div class="agents">
        <span class="lbl">Add agents</span>
        <div id="agent-list"></div>
      </div>
      <div class="actions"><button type="submit">Create</button></div>
    </form>
    <div id="rooms"></div>
    <div class="chat-root-drop" id="chat-root-drop">Move to top level</div>
  </div>
  <div class="room-main">
    <div class="room-title" id="room-title">
      <input type="text" id="room-title-name" autocomplete="off">
      <button type="button" id="rename-room-btn" class="rename-room" style="display:none">Rename</button>
      <select id="sidebar-mode" class="sidebar-mode" title="Right sidebar">
        <option value="hidden">Sidebar: off</option>
        <option value="members">Members</option>
        <option value="stats">Stats</option>
      </select>
    </div>
    <div class="chat-log" id="chat-log"></div>
    <form class="compose" id="compose" onsubmit="return false;">
      <textarea id="msg-input" rows="1" placeholder="Write a message…  (Enter to send, Shift+Enter for newline)"></textarea>
      <button type="submit">Send</button>
    </form>
  </div>
  <div class="chat-modal-backdrop" id="chat-modal-backdrop" hidden></div>

  <div class="chat-modal" id="chat-folder-modal" hidden>
    <h3 id="chat-folder-title">New folder</h3>
    <input type="text" id="chat-folder-input" placeholder="Folder name" autocomplete="off">
    <div class="modal-actions">
      <button type="button" class="btn-cancel" id="chat-folder-cancel">Cancel</button>
      <button type="button" class="btn-primary" id="chat-folder-create" disabled>Create</button>
    </div>
  </div>

  <div class="chat-modal" id="chat-delete-modal" hidden>
    <h3 id="chat-delete-title">Delete</h3>
    <p id="chat-delete-msg"></p>
    <p style="margin-bottom:0.3em">Type <strong id="chat-delete-name"></strong> to confirm:</p>
    <input type="text" id="chat-delete-input" autocomplete="off">
    <div class="modal-actions">
      <button type="button" class="btn-cancel" id="chat-delete-cancel">Cancel</button>
      <button type="button" class="btn-danger" id="chat-delete-confirm" disabled>Delete</button>
    </div>
  </div>

  <div class="room-sidebar" id="room-sidebar"></div>
</div>

<!-- Client-side markdown (marked) + sanitize (DOMPurify) + JSON syntax highlighting (highlight.js). -->
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/styles/github.min.css">
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/highlight.min.js"></script>
<script>
const roomsEl = document.getElementById('rooms');
const log = document.getElementById('chat-log');
const titleEl = document.getElementById('room-title');
const titleNameEl = document.getElementById('room-title-name');
const renameBtn = document.getElementById('rename-room-btn');
const sidebarEl = document.getElementById('room-sidebar');
const sidebarModeSel = document.getElementById('sidebar-mode');
const splitEl = document.querySelector('.chat-split');
const form = document.getElementById('compose');
const input = document.getElementById('msg-input');
const newRoomBtn = document.getElementById('new-room-btn');
const newRoomForm = document.getElementById('new-room');
const roomNameInput = document.getElementById('room-name');
const agentListEl = document.getElementById('agent-list');

// Lucide icons (https://lucide.dev/) — inline SVG so `stroke="currentColor"`
// inherits the button's text color, and width/height in em scales with
// the button's font-size. These are the upstream icons verbatim.
const LUCIDE_COPY_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>';
const LUCIDE_THUMBS_UP_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/><path d="M7 10v12"/></svg>';
const LUCIDE_THUMBS_DOWN_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/><path d="M17 14V2"/></svg>';

let rooms = [];                 // [{uuid, name, member_count, last_message_id}]
let currentRoom = null;         // uuid of the open room
let lastId = 0;                 // highest message id rendered in currentRoom
let renderedIds = new Set();    // message ids already in the log (dedup)
let streamingBase = {};         // message id -> last full row dict (for live in-place updates)
let expandedSections = new Set(); // collapsible-row ids (thinking/debug) the user expanded
const unread = {};              // room uuid -> unread count (rooms not open)
let deferredUnreadRender = false;
let deferredDeletedMessageIds = new Set();
let agentsLoaded = false;
const SIDEBAR_MODE_KEY = 'chat.sidebarMode';
let sidebarMode = 'hidden';     // 'hidden' | 'members' | 'stats'
try {
  const saved = localStorage.getItem(SIDEBAR_MODE_KEY);
  if (saved === 'hidden' || saved === 'members' || saved === 'stats') sidebarMode = saved;
} catch (e) {}
sidebarModeSel.value = sidebarMode;

// Append a message unless it's already rendered. A message can arrive via two
// racing paths (the post's own fetchNew and the SSE push), so dedup by id
// rather than relying on the `after` cursor being current.
function appendMessage(m){
  if (renderedIds.has(m.id)) return;
  renderedIds.add(m.id);
  log.appendChild(makeMessage(m));
  lastId = Math.max(lastId, m.id);
}

function removeDeletedMessages(ids){
  ids.forEach((id) => {
    const node = log.querySelector('[data-message-id="' + id + '"]');
    if (node) node.remove();
    renderedIds.delete(id);
  });
}

function isNearBottom(){
  return log.scrollHeight - log.scrollTop - log.clientHeight < 80;
}

// Insert a message node, or replace it in place if its id is already shown.
// Used for live streaming updates (the same row's text grows over time).
function upsertMessage(m){
  const existing = log.querySelector('[data-message-id="' + m.id + '"]');
  const pinned = isNearBottom();
  const node = makeMessage(m);
  if (existing){
    existing.replaceWith(node);
  } else {
    renderedIds.add(m.id);
    lastId = Math.max(lastId, m.id);
    log.appendChild(node);
  }
  if (pinned) log.scrollTop = log.scrollHeight;
}

// Handle a streaming NOTIFY ({message_id, kind, streaming, text?}): grow the
// thinking/answer bubble in place. The notify inlines `text` when small; when
// it's absent (first sighting, or text too large to inline) we fetch the
// authoritative row by id. Keeps a base dict per id so subsequent inlined
// updates need no HTTP.
async function applyStreamingUpdate(d){
  let base = streamingBase[d.message_id];
  if (!base || d.text === undefined){
    let m;
    try { m = await getJSON('/chat/api/rooms/' + d.room_uuid + '/messages/' + d.message_id); }
    catch (_) { return; }
    if (d.room_uuid !== currentRoom) return;
    base = m;
  } else {
    base = Object.assign({}, base, {text: d.text, streaming: d.streaming, kind: d.kind});
  }
  streamingBase[d.message_id] = base;
  upsertMessage(base);
  if (!base.streaming) delete streamingBase[d.message_id];
}

async function getJSON(url){
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + ' -> ' + r.status);
  return r.json();
}
async function postJSON(url, body){
  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(url + ' -> ' + r.status);
  return r.json();
}

// Send an up/down rating on a single agent message. The button row's data
// attributes carry the message uuid and the current selection. Disables
// both buttons during the request to prevent double-post; on success, the
// chosen button gets a `.fb-selected-up` or `.fb-selected-down` class.
async function ppPostFeedback(rowEl, rating){
  const messageUuid = rowEl.dataset.messageUuid;
  const buttons = rowEl.querySelectorAll('.fb-btn');
  buttons.forEach(b => b.disabled = true);
  try {
    const resp = await fetch('/chat/api/messages/' + messageUuid + '/feedback', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({rating: rating, comment: ''}),
    });
    if (!resp.ok) throw new Error('feedback ' + resp.status);
    // Clear any prior selection, then mark the new one.
    const upBtn = rowEl.querySelector('.fb-btn[data-fb=up]');
    const dnBtn = rowEl.querySelector('.fb-btn[data-fb=down]');
    if (upBtn) upBtn.classList.remove('fb-selected-up');
    if (dnBtn) dnBtn.classList.remove('fb-selected-down');
    const sel = rating === 'upvote' ? upBtn : dnBtn;
    if (sel) sel.classList.add(rating === 'upvote' ? 'fb-selected-up' : 'fb-selected-down');
  } catch (e) {
    alert('Feedback failed: ' + e.message);
  } finally {
    buttons.forEach(b => b.disabled = false);
  }
}

// Render markdown to sanitized HTML. DOMPurify strips dangerous markup
// (scripts, event handlers, etc.) so message text can't inject. Falls back to
// plain escaped text if the CDN libs failed to load.
function renderMarkdown(src){
  if (window.marked && window.DOMPurify){
    return DOMPurify.sanitize(marked.parse(src));
  }
  const tmp = document.createElement('div');
  tmp.textContent = src;
  return tmp.innerHTML;
}

function fallbackCopy(text, done){
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } catch (e) { /* ignore */ }
  document.body.removeChild(ta);
  done();
}

function copyText(text, btn){
  const done = () => {
    // The button may hold an SVG child instead of text; snapshot innerHTML
    // and restore that, otherwise the SVG gets wiped by textContent and the
    // button comes back blank.
    const prev = btn.innerHTML;
    btn.textContent = 'Copied';
    setTimeout(() => { btn.innerHTML = prev; }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

// Pretty-print JSON for display; fall back to the raw text if it doesn't parse.
function prettyJson(text){
  try { return JSON.stringify(JSON.parse(text), null, 2); }
  catch (_) { return text; }
}

// Apply highlight.js JSON syntax coloring to a <code> element. hljs escapes the
// content itself, so this stays XSS-safe; no-op if the CDN didn't load.
function highlightJson(codeEl){
  codeEl.classList.add('language-json');
  if (window.hljs) hljs.highlightElement(codeEl);
}

// Pretty-print any markdown code block whose contents are valid JSON.
function prettyPrintJsonBlocks(rootEl){
  rootEl.querySelectorAll('pre code').forEach((code) => {
    try {
      code.textContent = JSON.stringify(JSON.parse(code.textContent), null, 2);
      highlightJson(code);
    } catch (_) { /* not JSON — leave it as-is */ }
  });
}

// Copies the original markdown source, not the rendered HTML. Appends
// into a container (typically the .msg-actions row) rather than the
// .msg directly so the copy + feedback buttons share one parent and one
// margin-top.
function addCopyButton(container, source){
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'copy-btn';
  btn.title = 'Copy';
  btn.innerHTML = LUCIDE_COPY_SVG;
  btn.addEventListener('click', () => copyText(source, btn));
  container.appendChild(btn);
}

function makeMessage(m){
  const msg = document.createElement('div');
  // Anything other than a real "message" (e.g. the router's "debug-router"
  // output, or "thinking", or in-flight "progress") renders as a muted
  // debug bubble with a kind badge.
  const isDebug = m.kind && m.kind !== 'message';
  msg.className = isDebug ? 'msg msg-debug' : 'msg';
  // Collapsible rows and the noun used in their toggle labels: reasoning rows
  // collapse "thoughts", any debug-* row collapses "debug". Other kinds
  // (message, progress) aren't collapsible.
  const collapseNoun = m.kind === 'thinking' ? 'thoughts'
    : (m.kind && m.kind.indexOf('debug-') === 0) ? 'debug' : null;
  // A row still streaming gets a live cursor (CSS) and no feedback buttons yet.
  if (m.streaming) msg.classList.add('msg-streaming');
  // Tag the DOM node with its message id so the SSE handler can remove it
  // when the server reports it was deleted (used for progress rows).
  msg.dataset.messageId = String(m.id);

  const head = document.createElement('div');
  head.className = 'msg-head';
  const s = document.createElement('span');
  s.className = 'msg-sender';
  s.textContent = m.sender_name;
  const badge = document.createElement('span');
  badge.className = 'msg-type msg-type-' + m.sender_type;
  badge.textContent = m.sender_type;
  const t = document.createElement('span');
  t.className = 'msg-time';
  t.textContent = m.timestamp;
  head.appendChild(s);
  head.appendChild(badge);
  head.appendChild(t);
  if (isDebug){
    const k = document.createElement('span');
    k.className = 'msg-kind';
    k.textContent = m.kind;
    head.appendChild(k);
  }

  const body = document.createElement('div');
  body.className = 'msg-text';
  if (m.content_type === 'json'){
    // Render JSON in a code block. textContent (not innerHTML) keeps it safe.
    const pre = document.createElement('pre');
    const code = document.createElement('code');
    code.textContent = prettyJson(m.text);
    highlightJson(code);
    pre.appendChild(code);
    body.appendChild(pre);
  } else {
    body.innerHTML = renderMarkdown(m.text);
    prettyPrintJsonBlocks(body);
  }

  msg.appendChild(head);
  // Collapsible rows (thinking / debug-*) are collapsed by default. A toggle
  // sits above the body, and a second one below it (only while expanded) so a
  // long block can be collapsed from either end. The expanded set is keyed by
  // message id so the state survives the live re-renders while streaming.
  let toggleTop = null, toggleBottom = null;
  if (collapseNoun){
    toggleTop = document.createElement('button');
    toggleTop.type = 'button';
    toggleTop.className = 'thinking-toggle';
    msg.appendChild(toggleTop);
  }
  msg.appendChild(body);
  if (collapseNoun){
    toggleBottom = document.createElement('button');
    toggleBottom.type = 'button';
    toggleBottom.className = 'thinking-toggle';
    msg.appendChild(toggleBottom);
  }
  // Actions row: copy on the left, feedback ▲/▼ to its right. One
  // container owns the gap above the buttons so both share the same Y.
  const actions = document.createElement('div');
  actions.className = 'msg-actions';
  addCopyButton(actions, m.text);
  // Feedback row: only on agent user-facing replies. Never on human
  // messages or diagnostic rows (debug-memory / debug-query / progress /
  // thinking) — those aren't conversation outputs.
  if (!isDebug && m.sender_type === 'agent' && m.kind === 'message' && !m.streaming){
    const fb = document.createElement('div');
    fb.className = 'fb-row';
    fb.dataset.messageUuid = m.uuid;
    const up = document.createElement('button');
    up.type = 'button';
    up.className = 'fb-btn';
    up.dataset.fb = 'up';
    up.title = 'Upvote';
    up.innerHTML = LUCIDE_THUMBS_UP_SVG;
    up.addEventListener('click', () => ppPostFeedback(fb, 'upvote'));
    const dn = document.createElement('button');
    dn.type = 'button';
    dn.className = 'fb-btn';
    dn.dataset.fb = 'down';
    dn.title = 'Downvote';
    dn.innerHTML = LUCIDE_THUMBS_DOWN_SVG;
    dn.addEventListener('click', () => ppPostFeedback(fb, 'downvote'));
    fb.appendChild(up);
    fb.appendChild(dn);
    // Restore prior vote state from the server (so a reload shows it).
    if (m.feedback === 'upvote')   up.classList.add('fb-selected-up');
    if (m.feedback === 'downvote') dn.classList.add('fb-selected-down');
    actions.appendChild(fb);
  }
  msg.appendChild(actions);
  if (toggleTop){
    const apply = (expanded) => {
      toggleTop.textContent = expanded ? ('Collapse to hide ' + collapseNoun) : ('Expand to view ' + collapseNoun);
      toggleBottom.textContent = 'Collapse to hide ' + collapseNoun;
      body.style.display = expanded ? '' : 'none';
      actions.style.display = expanded ? '' : 'none';
      // The bottom toggle is only meaningful once the body is shown.
      toggleBottom.style.display = expanded ? '' : 'none';
    };
    const toggle = () => {
      const expanded = !expandedSections.has(m.id);
      if (expanded) expandedSections.add(m.id); else expandedSections.delete(m.id);
      apply(expanded);
    };
    apply(expandedSections.has(m.id));
    toggleTop.addEventListener('click', toggle);
    toggleBottom.addEventListener('click', toggle);
  }
  return msg;
}

function renderRooms(){
  roomsEl.innerHTML = '';
  if (!rooms.length){
    const p = document.createElement('p');
    p.className = 'note';
    p.textContent = 'No rooms yet — create one above.';
    roomsEl.appendChild(p);
    return;
  }
  rooms.forEach(r => {
    const isActive = r.uuid === currentRoom;
    const row = document.createElement('div');
    row.className = 'room-row' + (isActive ? ' active' : '');
    const btn = document.createElement('button');
    btn.className = 'room' + (isActive ? ' active' : '');
    btn.type = 'button';
    btn.dataset.room = r.uuid;
    const name = document.createElement('span');
    name.className = 'room-name';
    name.textContent = '# ' + r.name;
    const sub = document.createElement('span');
    sub.className = 'room-sub';
    sub.textContent = r.member_count + (r.member_count === 1 ? ' member' : ' members');
    btn.appendChild(name);
    btn.appendChild(sub);
    const n = unread[r.uuid] || 0;
    if (n > 0){
      const dot = document.createElement('span');
      dot.className = 'unread';
      dot.textContent = n;
      btn.appendChild(dot);
    }
    btn.addEventListener('click', () => selectRoom(r.uuid));
    row.appendChild(btn);
    if (isActive) row.appendChild(buildRoomMenu(r.uuid));
    roomsEl.appendChild(row);
  });
}

// The selected room's overflow (...) menu. Rename/Mute/Archive are placeholders
// (they just close the menu); Delete confirms and removes the room.
function buildRoomMenu(roomUuid){
  const wrap = document.createElement('div');
  wrap.className = 'room-actions';
  const kebab = document.createElement('button');
  kebab.type = 'button';
  kebab.className = 'room-kebab';
  kebab.setAttribute('aria-label', 'Room actions');
  kebab.setAttribute('aria-haspopup', 'menu');
  // Dots are drawn via CSS (.room-kebab::before) so they sit exactly on mid-y.
  const menu = document.createElement('div');
  menu.className = 'room-menu';
  menu.setAttribute('role', 'menu');
  menu.hidden = true;
  // Only wired-up items are shown (Rename/Mute/Archive are not implemented yet).
  [['Delete', 'danger']].forEach(([label, mod]) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'item' + (mod ? ' ' + mod : '');
    item.setAttribute('role', 'menuitem');
    item.textContent = label;
    item.addEventListener('click', (e) => {
      e.stopPropagation();
      menu.hidden = true;
      if (label === 'Delete') deleteRoom(roomUuid);
    });
    menu.appendChild(item);
  });
  kebab.addEventListener('click', (e) => {
    e.stopPropagation();  // don't let the row's click re-select / dismiss
    const willOpen = menu.hidden;
    document.querySelectorAll('.room-menu').forEach(m => { m.hidden = true; });
    if (willOpen){
      // Anchor the fixed menu under the kebab, left edges aligned.
      const r = kebab.getBoundingClientRect();
      menu.style.left = r.left + 'px';
      menu.style.top = (r.bottom + 4) + 'px';
      menu.hidden = false;
    }
  });
  wrap.appendChild(kebab);
  wrap.appendChild(menu);
  return wrap;
}

async function deleteRoom(uuid){
  const room = rooms.find(r => r.uuid === uuid);
  const label = room ? '# ' + room.name : 'this room';
  if (!confirm('Delete ' + label + ' and all its messages? This cannot be undone.')) return;
  try {
    const r = await fetch('/chat/api/rooms/' + uuid, { method: 'DELETE' });
    if (!r.ok) throw new Error('DELETE ' + uuid + ' -> ' + r.status);
  } catch (e) { alert(e); return; }
  rooms = rooms.filter(x => x.uuid !== uuid);
  delete unread[uuid];
  if (currentRoom === uuid){
    currentRoom = null;
    if (rooms[0]){ await selectRoom(rooms[0].uuid); return; }
    // No rooms left: clear the main pane and drop ?room= from the URL.
    titleNameEl.value = '';
    log.innerHTML = '';
    const url = new URL(window.location);
    url.searchParams.delete('room');
    history.replaceState(null, '', url);
    renderSidebar();
  }
  renderRooms();
}

// Dismiss any open room overflow menu on an outside click or Escape.
document.addEventListener('click', () => {
  document.querySelectorAll('.room-menu').forEach(m => { m.hidden = true; });
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') document.querySelectorAll('.room-menu').forEach(m => { m.hidden = true; });
});

async function selectRoom(uuid){
  currentRoom = uuid;
  unread[uuid] = 0;
  lastId = 0;
  renderedIds = new Set();
  streamingBase = {};
  expandedSections = new Set();
  // Remember the active room in the URL so a reload reopens it.
  const url = new URL(window.location);
  url.searchParams.set('room', uuid);
  history.replaceState(null, '', url);
  renderRooms();
  const room = rooms.find(r => r.uuid === uuid);
  titleNameEl.value = room ? room.name : '';
  log.innerHTML = '';
  input.focus();
  const msgs = await getJSON('/chat/api/rooms/' + uuid + '/messages?after=0');
  if (uuid !== currentRoom) return;  // room switched while loading
  msgs.forEach(appendMessage);
  log.scrollTop = log.scrollHeight;
  renderSidebar();  // members/stats reflect the now-open room
}

async function fetchNew(uuid){
  if (uuid !== currentRoom) return;
  const msgs = await getJSON('/chat/api/rooms/' + uuid + '/messages?after=' + lastId);
  if (uuid !== currentRoom || !msgs.length) return;  // re-check after await
  msgs.forEach(appendMessage);
  log.scrollTop = log.scrollHeight;
  if (sidebarMode === 'stats') renderStats();  // keep the live message count fresh
}

async function loadRooms(selectUuid){
  rooms = await getJSON('/chat/api/rooms');
  renderRooms();
  let target = selectUuid || currentRoom;
  // Fall back to the first room if the requested one is missing (e.g. a stale
  // ?room= uuid for a deleted room).
  if (!target || !rooms.some(r => r.uuid === target)){
    target = rooms[0] && rooms[0].uuid;
  }
  if (target) await selectRoom(target);
}

async function send(){
  const text = input.value.trim();
  if (!text || !currentRoom) return;
  input.value = '';
  autoGrow();  // collapse back to one line after sending
  await postJSON('/chat/api/rooms/' + currentRoom + '/messages', { text });
  await fetchNew(currentRoom);  // don't wait for the SSE round-trip
  input.focus();
}

form.addEventListener('submit', send);

async function doRenameRoom(){
  if (!currentRoom) return;
  const room = rooms.find(r => r.uuid === currentRoom);
  const name = (titleNameEl.value || '').trim();
  if (!name){ alert('name cannot be empty'); return; }
  if (room && name === room.name) return;
  try {
    await postJSON('/chat/api/rooms/' + currentRoom + '/rename', { name });
    if (room) room.name = name;
    renderRooms();  // reflect the new name in the left panel
    titleNameEl.blur();
  } catch (e) { alert(e); }
}

// The Rename button only shows while the title field is focused (less noise).
// preventDefault on mousedown keeps the input focused so the click lands before
// blur would hide the button.
titleNameEl.addEventListener('focus', () => { renameBtn.style.display = ''; });
titleNameEl.addEventListener('blur', () => { renameBtn.style.display = 'none'; });
renameBtn.addEventListener('mousedown', (e) => { e.preventDefault(); });
renameBtn.addEventListener('click', doRenameRoom);
titleNameEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter'){ e.preventDefault(); doRenameRoom(); }
});

// Right sidebar: hidden / members / stats.
async function renderSidebar(){
  if (sidebarMode === 'hidden' || !currentRoom){
    splitEl.classList.remove('sidebar-open');
    sidebarEl.innerHTML = '';
    return;
  }
  splitEl.classList.add('sidebar-open');
  if (sidebarMode === 'members') await renderMembers();
  else if (sidebarMode === 'stats') renderStats();
}

async function renderMembers(){
  const room = currentRoom;
  let members, agents;
  try {
    [members, agents] = await Promise.all([
      getJSON('/chat/api/rooms/' + room + '/members'),
      getJSON('/chat/api/agents'),
    ]);
  } catch (_) { return; }
  if (room !== currentRoom || sidebarMode !== 'members') return;  // changed while loading
  const memberUuids = new Set(members.map(m => m.uuid));
  const humans = members.filter(m => m.user_type === 'human');
  sidebarEl.innerHTML = '';
  const h = document.createElement('h3');
  h.className = 'sidebar-title';
  h.textContent = 'Members (' + members.length + ')';
  sidebarEl.appendChild(h);
  const ul = document.createElement('ul');
  ul.className = 'member-list';
  // Humans: always members, rendered read-only (no toggle).
  humans.forEach(m => {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'member-name';
    name.textContent = m.name;
    const badge = document.createElement('span');
    badge.className = 'msg-type msg-type-' + m.user_type;
    badge.textContent = m.user_type;
    li.appendChild(name);
    li.appendChild(badge);
    ul.appendChild(li);
  });
  // Agents: every agent is a checkbox; checked = member. Toggling adds/removes live.
  agents.forEach(a => {
    const li = document.createElement('li');
    const label = document.createElement('label');
    label.className = 'member-toggle';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = a.uuid;
    cb.checked = memberUuids.has(a.uuid);
    cb.addEventListener('change', () => toggleMember(room, a.uuid, cb));
    const name = document.createElement('span');
    name.className = 'member-name';
    name.textContent = a.name;
    label.appendChild(cb);
    label.appendChild(name);
    li.appendChild(label);
    ul.appendChild(li);
  });
  sidebarEl.appendChild(ul);
}

// Add (checkbox now checked) or remove (now unchecked) an agent from a room.
// Optimistic: the checkbox is already flipped; on failure we revert it.
async function toggleMember(room, agentUuid, cb){
  const wantMember = cb.checked;
  cb.disabled = true;
  try {
    let resp;
    if (wantMember){
      resp = await fetch('/chat/api/rooms/' + room + '/members', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({user_uuid: agentUuid}),
      });
    } else {
      resp = await fetch('/chat/api/rooms/' + room + '/members/' + agentUuid, {
        method: 'DELETE',
      });
    }
    if (!resp.ok) throw new Error('member toggle -> ' + resp.status);
    // Reflect the new count in the left room list locally (no full reload).
    const r = rooms.find(x => x.uuid === room);
    if (r){ r.member_count += wantMember ? 1 : -1; renderRooms(); }
    // Rebuild the panel so the heading count stays accurate (also re-enables).
    if (room === currentRoom && sidebarMode === 'members') renderMembers();
  } catch (e) {
    cb.checked = !wantMember;  // revert on failure
    cb.disabled = false;
  }
}

function statRow(label, value){
  const d = document.createElement('div');
  d.className = 'stat';
  const s = document.createElement('span'); s.textContent = label;
  const b = document.createElement('b'); b.textContent = value;
  d.appendChild(s); d.appendChild(b);
  return d;
}

function renderStats(){
  const room = rooms.find(r => r.uuid === currentRoom);
  sidebarEl.innerHTML = '';
  const h = document.createElement('h3');
  h.className = 'sidebar-title';
  h.textContent = 'Stats';
  sidebarEl.appendChild(h);
  // renderedIds holds every message loaded for the open room, so its size is a
  // live count that grows as new messages arrive.
  sidebarEl.appendChild(statRow('Messages', renderedIds.size));
  sidebarEl.appendChild(statRow('Members', room ? room.member_count : 0));
}

sidebarModeSel.addEventListener('change', () => {
  sidebarMode = sidebarModeSel.value;
  try { localStorage.setItem(SIDEBAR_MODE_KEY, sidebarMode); } catch (e) {}
  renderSidebar();
});

// Grow the textarea to fit its content (CSS max-height caps it at 10 rows and
// switches to scrolling beyond that). Reset to 'auto' first so it can shrink.
function autoGrow(){
  input.style.height = 'auto';
  input.style.height = input.scrollHeight + 'px';
}
input.addEventListener('input', autoGrow);
autoGrow();

// Enter sends; Shift+Enter inserts a newline (textarea default).
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey){
    e.preventDefault();
    send();
  }
});

async function loadAgents(){
  const agents = await getJSON('/chat/api/agents');
  agentListEl.innerHTML = '';
  agents.forEach(a => {
    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = a.uuid;
    label.appendChild(cb);
    label.appendChild(document.createTextNode(a.name));
    agentListEl.appendChild(label);
  });
  agentsLoaded = true;
}

newRoomBtn.addEventListener('click', async () => {
  newRoomForm.classList.toggle('hidden');
  if (!newRoomForm.classList.contains('hidden')){
    if (!agentsLoaded) await loadAgents();
    roomNameInput.focus();
  }
});

newRoomForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = roomNameInput.value.trim();
  // The `required` attribute already blocks an empty name with native validation,
  // but double-check defensively.
  if (!name){ roomNameInput.focus(); return; }
  const member_uuids = Array.from(agentListEl.querySelectorAll('input:checked')).map(cb => cb.value);
  try {
    const res = await postJSON('/chat/api/rooms', { name, member_uuids });
    roomNameInput.value = '';
    agentListEl.querySelectorAll('input:checked').forEach(cb => { cb.checked = false; });
    newRoomForm.classList.add('hidden');
    await loadRooms(res.uuid);
  } catch (err) {
    alert('Create room failed: ' + err);
  }
});

// Live updates: the server pushes {room_uuid, message_id} on every new message.
// EventSource ignores `:` comment lines (the keepalives), so only real messages
// reach onmessage.
function startStream(){
  const es = new EventSource('/chat/stream');
  // On every (re)connect, pull anything posted while the stream was down — so a
  // dropped connection can't silently strand messages (e.g. an agent reply that
  // would otherwise only surface on your next post).
  es.onopen = () => {
    if (!currentRoom) return;
    if (document.hidden) return;
    fetchNew(currentRoom);
  };
  es.onmessage = (e) => {
    let d;
    try { d = JSON.parse(e.data); } catch (_) { return; }
    if (!d.room_uuid) return;
    if (d.room_uuid === currentRoom){
      const deleted = Array.isArray(d.deleted_progress_ids) ? d.deleted_progress_ids : [];
      if (document.hidden){
        deleted.forEach((id) => deferredDeletedMessageIds.add(id));
        return;
      }
      // Remove any progress rows the server just deleted (auto-cleared when
      // the agent posted its real reply). Drop them from the DOM and from
      // renderedIds so they don't linger or block a future re-add.
      removeDeletedMessages(deleted);
      // A streaming notify carries the `streaming` flag — update that one bubble
      // in place. Everything else uses the append-after-cursor path.
      if (d.streaming !== undefined){
        applyStreamingUpdate(d);
      } else {
        fetchNew(currentRoom);
      }
    } else {
      unread[d.room_uuid] = (unread[d.room_uuid] || 0) + 1;
      if (document.hidden){
        deferredUnreadRender = true;
        return;
      }
      renderRooms();
    }
  };
  es.onerror = () => {
    // While readyState is CONNECTING the browser is already retrying on its own;
    // only when it has given up (CLOSED) do we rebuild the stream ourselves.
    if (es.readyState === EventSource.CLOSED) setTimeout(startStream, 3000);
  };
}

// Browsers throttle/suspend EventSource in backgrounded tabs; catch up on refocus.
document.addEventListener('visibilitychange', () => {
  if (document.hidden || !currentRoom) return;
  if (deferredUnreadRender){
    deferredUnreadRender = false;
    renderRooms();
  }
  if (deferredDeletedMessageIds.size){
    removeDeletedMessages(deferredDeletedMessageIds);
    deferredDeletedMessageIds = new Set();
  }
  // Reconcile any rows that were mid-stream while the tab was hidden (their
  // in-place updates were skipped): refetch each by id so it shows final text.
  const room = currentRoom;
  log.querySelectorAll('.msg-streaming').forEach((node) => {
    const id = node.dataset.messageId;
    if (!id) return;
    getJSON('/chat/api/rooms/' + room + '/messages/' + id)
      .then((m) => { if (room === currentRoom) upsertMessage(m); })
      .catch(() => {});
  });
  fetchNew(currentRoom);
});

loadRooms(new URLSearchParams(window.location.search).get('room'));
startStream();
</script>
"""
