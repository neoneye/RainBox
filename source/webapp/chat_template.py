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
<link rel="stylesheet" href="/static/ui-modal.css">
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  .chat-split{display:grid;grid-template-columns:260px 1fr;grid-template-rows:1fr;flex:1 1 auto;min-height:0}
  .chat-split.sidebar-open{grid-template-columns:260px 1fr 240px}

  .rooms{overflow:auto;min-height:0;border-right:1px solid #ddd;background:#fbfbfb;padding:0.5em;font-size:0.9rem}
  .rooms-head{display:flex;align-items:center;justify-content:space-between;padding:0.2em 0.4em 0.5em}
  .new-room-btn{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;
                padding:0.25em 0.6em;cursor:pointer;font:inherit;font-size:0.78rem;margin-left:0.4em}
  .new-room-btn:hover{border-color:#2563eb;color:#2563eb}
  .rooms .note{margin:0.2em 0.4em 0.6em;color:#888;font-size:0.78rem}


  .room{position:relative;display:block;width:100%;text-align:left;border:none;background:none;cursor:pointer;
        padding:0.5em 0.7em;border-radius:6px;font:inherit;color:#333;text-decoration:none;box-sizing:border-box}
  .room:hover{background:#eef0f6}
  .room.active{background:#e3ebfb;font-weight:600}  /* match the selected folder/kanban node */
  .room-name{display:block}
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
  /* Folder rows are anchors (CMD/Ctrl-click opens a new tab) — suppress link styling. */
  .chat-node{position:relative;display:flex;align-items:center;gap:0.4em;width:100%;box-sizing:border-box;
             padding:0.4em 0.6em;border-radius:6px;cursor:pointer;color:#333;font-size:0.9rem;text-decoration:none}
  .chat-node:hover{background:#eef0f6}
  .chat-node.sel{background:#dbeafe;font-weight:600}
  /* Folder kebab: hidden by default, shown only when the folder is selected
     (mirrors the rooms' active-only kebab and the /cron tree — no hover reveal).
     The kebab lives in a .room-actions wrap appended directly inside .chat-node
     by buildFolderMenu. */
  .chat-node > .room-actions{visibility:hidden}
  .chat-node.sel > .room-actions{visibility:visible}
  .chat-ficon{display:inline-flex;width:1.05em;height:1.05em;color:#6b7280;flex:0 0 auto}
  .chat-ficon svg{width:100%;height:100%}
  .chat-folder-label{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
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
  /* modal: shared base lives in /static/ui-modal.css; only chat-specific bits here */
  .ui-modal input[type=text]{width:100%;box-sizing:border-box;padding:0.5em;border:1px solid #ccc;
                               border-radius:6px;font:inherit}
  .ui-modal .agents{display:flex;flex-direction:column;gap:0.25em;margin:0.6em 0;max-height:30vh;overflow:auto}
  /* class-level display:flex beats the UA [hidden] rule; make hidden win (the
     agent picker is hidden while "Direct LLM chat" is the chosen room type) */
  .ui-modal .agents[hidden]{display:none}
  .ui-modal .agents .lbl{color:#888;font-size:0.75rem;margin-bottom:0.1em}
  .ui-modal .agents label{font-size:0.85rem;color:#333;display:flex;align-items:center;gap:0.4em}

  .room-main{display:flex;flex-direction:column;overflow:hidden;min-height:0}
  .room-title{padding:0.6em 1em;border-bottom:1px solid #eee;font-weight:600;display:flex;align-items:center;gap:0.6em}
  /* Click-to-rename room name: doubles as the title; clicking opens the
     rename modal (docs/ui-modal-rename.md). margin-right:auto pushes the
     sidebar-mode select to the bar's right edge. */
  .room-title button#room-title-name{font:inherit;font-size:1.05em;font-weight:600;color:#1a1a2e;background:none;
        text-align:left;border:1px solid transparent;border-radius:6px;padding:0.2em 0.4em;cursor:pointer;
        margin-right:auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .room-title button#room-title-name:hover{border-color:#cbd5e1;background:#f8fafc}
  .sidebar-mode{font:inherit;font-size:0.8rem;color:#6c757d;border:1px solid #ccc;border-radius:6px;padding:0.2em 0.4em;background:#fff;cursor:pointer}

  .room-sidebar{display:none;overflow:auto;min-height:0;border-left:1px solid #ddd;background:#fbfbfb;padding:0.8em 1em}
  .chat-split.sidebar-open .room-sidebar{display:block}
  .room-sidebar .sidebar-title{margin:0 0 0.7em;font-size:0.95rem;color:#333}
  .room-sidebar .member-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:0.45em}
  .room-sidebar .member-list li{display:flex;align-items:center;gap:0.5em;font-size:0.9rem;color:#333}
  .room-sidebar .member-name{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .room-sidebar .member-list li label.member-toggle{display:flex;align-items:center;gap:0.5em;flex:1 1 auto;cursor:pointer;margin:0}
  .room-sidebar .stat{display:flex;justify-content:space-between;padding:0.35em 0;font-size:0.9rem;border-bottom:1px solid #eee}

  /* Direct-room Settings sidebar (model picker + system prompt). */
  .room-sidebar .ds-label{display:block;margin:0.8em 0 0.25em;font-size:0.78rem;color:#6b7280;text-transform:uppercase;letter-spacing:0.03em}
  .room-sidebar select.ds-model{width:100%;box-sizing:border-box;font:inherit;font-size:0.85rem;padding:0.3em;border:1px solid #ccc;border-radius:6px;background:#fff}
  .room-sidebar input.ds-timeout{width:100%;box-sizing:border-box;font:inherit;font-size:0.85rem;padding:0.3em;border:1px solid #ccc;border-radius:6px;background:#fff}
  .room-sidebar textarea.ds-prompt{width:100%;box-sizing:border-box;font:inherit;font-size:0.85rem;line-height:1.4;padding:0.4em;border:1px solid #ccc;border-radius:6px;resize:vertical;min-height:12em}
  /* Linked stored prompt: the textarea becomes a read-only preview. */
  .room-sidebar textarea.ds-prompt:disabled{background:#f8fafc;color:#6b7280}
  /* Prompt-source row: linked prompt name (or "Custom text") + small buttons. */
  .room-sidebar .ds-prompt-mode{display:flex;align-items:center;gap:0.5em;margin:0 0 0.4em;font-size:0.85rem;flex-wrap:wrap}
  .room-sidebar .ds-prompt-mode .src{color:#374151}
  .room-sidebar .ds-prompt-mode a{color:#2563eb;text-decoration:none;max-width:100%;overflow:hidden;text-overflow:ellipsis}
  .room-sidebar .ds-prompt-mode a:hover{text-decoration:underline}
  .room-sidebar .ds-prompt-mode a.gone{color:#b91c1c}
  .room-sidebar .ds-prompt-mode button{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;padding:0.15em 0.55em;font:inherit;font-size:0.78rem;cursor:pointer}
  .room-sidebar .ds-prompt-mode button:hover{border-color:#2563eb;color:#2563eb}
  /* Stored-prompt picker modal: a read-only render of the /prompt folder tree. */
  .ui-modal .prompt-pick-tree{max-height:45vh;overflow:auto;border:1px solid #e5e7eb;border-radius:6px;padding:6px;margin:0.6em 0;font-size:0.9rem;background:#fbfbfb}
  .prompt-pick-tree ul{list-style:none;margin:0;padding:0}
  .prompt-pick-tree ul ul{margin-left:0.85em;border-left:1px solid #e5e7eb;padding-left:0.35em}
  .prompt-pick-node{display:flex;align-items:center;gap:4px;padding:6px 4px;border-radius:4px;cursor:pointer;white-space:nowrap;-webkit-user-select:none;user-select:none}
  .prompt-pick-node:hover{background:#f1f5f9}
  .prompt-pick-leaf{padding:4px 4px;border-radius:4px;cursor:pointer;color:#374151;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .prompt-pick-leaf:hover{background:#dbeafe}
  .prompt-pick-empty{color:#6b7280;padding:6px;font-size:0.85rem}
  .ui-modal .prompt-pick-hint{color:#888;font-size:0.82rem;margin:0.4em 0 0}
  .room-sidebar .ds-save{margin-top:0.8em;padding:0.4em 1em;font:inherit;font-size:0.85rem;border:1px solid #cbd5e1;border-radius:6px;background:#fff;color:#374151;cursor:pointer}
  .room-sidebar .ds-save:hover{background:#f1f5f9}
  .room-sidebar .ds-save:disabled{background:#f8fafc;color:#9ca3af;cursor:default}
  .room-sidebar .ds-note{color:#888;font-size:0.85rem}

  /* In-place message editing (direct rooms only). */
  .msg-edit-area{width:100%;box-sizing:border-box;font:inherit;font-size:0.95rem;line-height:1.4;padding:0.4em;border:1px solid #2563eb;border-radius:6px;resize:vertical;min-height:6em}
  .msg-edit-actions{display:flex;gap:0.4em;margin-top:0.4em}
  .msg-edit-actions .btn-save-edit{padding:0.3em 0.9em;font:inherit;font-size:0.85rem;border:none;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer}
  .msg-edit-actions .btn-save-edit:disabled{background:#9db4e8;cursor:default}
  .msg-edit-actions .btn-cancel-edit{padding:0.3em 0.9em;font:inherit;font-size:0.85rem;border:1px solid #ccc;border-radius:6px;background:#fff;color:#374151;cursor:pointer}

  /* Room-type choice in the new-room modal. */
  .ui-modal .room-type-choices{display:flex;gap:1.2em;margin:0.6em 0 0.2em}
  .ui-modal .room-type-choices label{font-size:0.85rem;color:#333;display:flex;align-items:center;gap:0.4em}

  .chat-log{flex:1 1 auto;overflow:auto;padding:1em;display:flex;flex-direction:column;gap:1.1em}
  /* Deep-link target flash (e.g. /assistant "open in chat"). */
  .msg.msg-highlight{animation:pp-msg-flash 2.6s ease-out}
  @keyframes pp-msg-flash{0%{background:#fde68a}30%{background:#fef3c7}100%{background:transparent}}
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
  /* assistant trace step (rendered from assistant_run/assistant_step rows) */
  .astep-head{font-weight:600;color:#4338ca}
  .astep-reason{color:#555;margin:2px 0}
  .astep-observation{margin:2px 0;white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:0.8rem;background:#eef2ff;border-radius:6px;padding:4px 8px}
  .astep-error{margin:2px 0;color:#b91c1c;white-space:pre-wrap}
  .astep-final{color:#15803d}
  .astep-label{font-weight:600;text-transform:uppercase;font-size:0.66rem;letter-spacing:0.03em;color:#6366f1}
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
  /* Diagnostic output is often JSON, reasoning, or tool output with long
     unbroken values. Keep ordinary chat code blocks horizontally scrollable,
     but wrap debug/thinking content to the available chat width. */
  .msg-debug .msg-text{min-width:0;overflow-wrap:anywhere;word-break:break-word}
  .msg-debug .msg-text pre{white-space:pre-wrap;overflow-wrap:anywhere;
                          word-break:break-word;overflow-x:hidden}
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

  /* Folder-contents table (shown in room-main instead of a chat). The chat-log
     and compose set display:flex at class specificity, which beats the UA
     [hidden]{display:none}, so the hidden attribute needs explicit rules to
     actually hide them when the folder table takes over the pane. */
  .chat-log[hidden]{display:none}
  .compose[hidden]{display:none}
  .room-title[hidden]{display:none}
  .folder-detail{flex:1 1 auto;overflow:auto;padding:1em}
  .folder-detail h2{margin:0 0 0.8em;font-size:1.1rem;color:#1a1a2e}
  .folder-detail table{width:100%;border-collapse:collapse;font-size:0.9rem}
  .folder-detail th,.folder-detail td{text-align:left;padding:0.45em 0.7em;border-bottom:1px solid #eee;vertical-align:top}
  .folder-detail th{color:#6b7280;font-weight:600;font-size:0.8rem;text-transform:uppercase;letter-spacing:0.03em}
  .folder-detail td.fd-num{text-align:right;white-space:nowrap}
  .folder-detail .fd-name{white-space:nowrap}
  .folder-detail .fd-icon{color:#6b7280;margin-right:0.3em}
  .folder-detail a.fd-details{color:#2563eb;cursor:pointer;text-decoration:none}
  .folder-detail a.fd-details:hover{text-decoration:underline}
  .folder-detail .fd-empty{color:#888;font-style:italic}
  /* Transient bottom-right toast (e.g. "Room id copied: …"), matching /cron,
     /kanban and /memory. */
  .chat-toast{position:fixed;bottom:18px;right:18px;max-width:380px;background:#1f2937;color:#fff;
    padding:10px 14px;border-radius:8px;font-size:0.9rem;box-shadow:0 4px 14px rgba(0,0,0,0.3);
    z-index:2000;opacity:0;transition:opacity .25s;pointer-events:none}
  .chat-toast.show{opacity:1}
  /* Write-proposal card (confirm/reject) */
  .write-proposal{margin-top:.4rem;padding:.4rem .6rem;border:1px solid #d1d5db;
    border-radius:6px;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
  .write-proposal .wp-cap{font-weight:600}
  .write-proposal button{cursor:pointer}
  .write-proposal .wp-confirm{background:#2563eb;color:#fff;border:none;
    border-radius:4px;padding:.2rem .6rem}
  .write-proposal .wp-reject{background:#fff;color:#b91c1c;border:1px solid #b91c1c;
    border-radius:4px;padding:.2rem .6rem}
  .write-proposal .wp-completed{color:#15803d}
  .write-proposal .wp-rejected{color:#b91c1c}
  .write-proposal .wp-failed{color:#b45309}
  .write-proposal .wp-step{margin-left:auto;font-size:.85em}
  .write-proposal .wp-result-link{margin-left:.6rem;font-size:.85em}
  .write-proposal .wp-result{flex-basis:100%;font-size:.85em}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="chat-split">
  <div class="rooms">
    <div class="rooms-head">
      <span>
        <button class="new-folder-btn" id="new-folder-btn" type="button">+ Folder</button>
        <button class="new-room-btn" id="new-room-btn" type="button">+ New room</button>
      </span>
    </div>
    <div id="rooms"></div>
    <div class="chat-root-drop" id="chat-root-drop">Move to top level</div>
  </div>
  <div class="room-main">
    <div class="room-title" id="room-title">
      <button type="button" id="room-title-name" title="Click to rename"></button>
      <select id="sidebar-mode" class="sidebar-mode" title="Right sidebar — Ctrl+1 toggles">
        <option value="hidden">Sidebar: off</option>
        <option value="members">Members</option>
        <option value="stats">Stats</option>
        <option value="settings">Settings</option>
        <option value="export">Export</option>
      </select>
    </div>
    <div class="chat-log" id="chat-log"></div>
    <div class="folder-detail" id="folder-detail" hidden>
      <h2 id="folder-detail-title"></h2>
      <table>
        <thead>
          <tr><th>Name</th><th>Agents</th><th class="fd-num">Messages</th><th>Last message</th><th></th></tr>
        </thead>
        <tbody id="folder-detail-rows"></tbody>
      </table>
    </div>
    <form class="compose" id="compose" onsubmit="return false;">
      <textarea id="msg-input" rows="1" placeholder="Write a message…  (Enter to send, Shift+Enter for newline)"></textarea>
      <button type="submit">Send</button>
    </form>
  </div>
  <div class="ui-modal-backdrop" id="ui-modal-backdrop" hidden></div>

  <div class="ui-modal" id="chat-folder-modal" hidden>
    <h3 id="chat-folder-title">New folder</h3>
    <input type="text" id="chat-folder-input" placeholder="Folder name" autocomplete="off">
    <div class="modal-actions">
      <button type="button" class="btn-cancel" id="chat-folder-cancel">Cancel</button>
      <button type="button" class="btn-primary" id="chat-folder-create" disabled>Create</button>
    </div>
  </div>

  <div class="ui-modal" id="chat-rename-modal" hidden>
    <h3>Rename room</h3>
    <input type="text" id="chat-rename-input" autocomplete="off">
    <div class="modal-actions">
      <button type="button" class="btn-cancel" id="chat-rename-cancel">Cancel</button>
      <button type="button" class="btn-primary" id="chat-rename-confirm" disabled>Rename</button>
    </div>
  </div>

  <div class="ui-modal" id="chat-delete-modal" hidden>
    <h3 id="chat-delete-title">Delete</h3>
    <p id="chat-delete-msg"></p>
    <p style="margin-bottom:0.3em">Type <strong id="chat-delete-name"></strong> to confirm:</p>
    <input type="text" id="chat-delete-input" autocomplete="off">
    <div class="modal-actions">
      <button type="button" class="btn-cancel" id="chat-delete-cancel">Cancel</button>
      <button type="button" class="btn-danger" id="chat-delete-confirm" disabled>Delete</button>
    </div>
  </div>

  <div class="ui-modal" id="chat-room-modal" hidden>
    <h3>New chatroom</h3>
    <input type="text" id="chat-room-input" placeholder="Room name" autocomplete="off">
    <div class="room-type-choices">
      <label><input type="radio" name="chat-room-type" value="agents" checked> Agents room</label>
      <label><input type="radio" name="chat-room-type" value="direct"> Direct LLM chat</label>
    </div>
    <div class="agents" id="chat-room-agents">
      <span class="lbl">Add agents</span>
      <div id="agent-list"></div>
    </div>
    <div class="modal-actions">
      <button type="button" class="btn-cancel" id="chat-room-cancel">Cancel</button>
      <button type="button" class="btn-primary" id="chat-room-create" disabled>Create</button>
    </div>
  </div>

  <div class="ui-modal" id="chat-prompt-modal" hidden>
    <h3>Choose system prompt</h3>
    <div class="prompt-pick-tree" id="chat-prompt-tree"></div>
    <p class="prompt-pick-hint">Stored prompts are managed on the
      <a href="/prompt" target="_blank">Prompt</a> page. Click one to link it
      to this chat; its current content is used from the next reply on.</p>
    <div class="modal-actions">
      <button type="button" class="btn-cancel" id="chat-prompt-cancel">Cancel</button>
    </div>
  </div>

  <div class="room-sidebar" id="room-sidebar"></div>
</div>
<div id="chat-toast" class="chat-toast"></div>

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
const sidebarEl = document.getElementById('room-sidebar');
const sidebarModeSel = document.getElementById('sidebar-mode');
const splitEl = document.querySelector('.chat-split');
const form = document.getElementById('compose');
const input = document.getElementById('msg-input');
const newRoomBtn = document.getElementById('new-room-btn');
const agentListEl = document.getElementById('agent-list');

// Lucide icons (https://lucide.dev/) — inline SVG so `stroke="currentColor"`
// inherits the button's text color, and width/height in em scales with
// the button's font-size. These are the upstream icons verbatim.
const LUCIDE_COPY_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>';
const LUCIDE_THUMBS_UP_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/><path d="M7 10v12"/></svg>';
const LUCIDE_THUMBS_DOWN_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/><path d="M17 14V2"/></svg>';
const LUCIDE_MORE_HORIZONTAL_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/></svg>';
const LUCIDE_PENCIL_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/></svg>';
const LUCIDE_TRASH_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/></svg>';
const CHAT_ICON_FOLDER = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>';
const CHAT_ICON_FOLDER_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>';

let rooms = [];                 // [{uuid, name, member_count, last_message_id, room_type, model_uuid}]
let folders = [];               // [{id, name, parentId}]
let treeVersion = null;         // optimistic-concurrency token from /chat/api/tree
let chatDefaultModel = null;    // global chat.default_model uuid (rooms without a model use it)
let dragNode = null;            // {type:'folder'|'room', id} during a drag
const FOLDER_EXPAND_KEY = 'chat.expandedFolders';
let expandedFolders = {};       // folderId -> false when collapsed (default expanded)
try {
  const saved = JSON.parse(localStorage.getItem(FOLDER_EXPAND_KEY) || '{}');
  if (saved && typeof saved === 'object') expandedFolders = saved;
} catch (e) {}
function saveExpandState(){
  try { localStorage.setItem(FOLDER_EXPAND_KEY, JSON.stringify(expandedFolders)); } catch (e) {}
}
function folderById(id){ return folders.find(f => f.id === id) || null; }
function childFolders(parentId){ return folders.filter(f => (f.parentId || null) === parentId); }
function roomsInFolder(id){ return rooms.filter(r => (r.folderId || null) === id); }
// The top-most room in left-panel render order: depth-first, subfolders before
// rooms (mirrors renderRooms). Used as the default selection — NOT rooms[0],
// which is global position order and can sit below entire folders in the tree.
function firstRoomInTree(parentId){
  parentId = parentId || null;
  for (const f of childFolders(parentId)){
    const r = firstRoomInTree(f.id);
    if (r) return r;
  }
  const rs = roomsInFolder(parentId);
  return rs.length ? rs[0].uuid : null;
}
function isExpanded(id){ return expandedFolders[id] !== false; }
let currentRoom = null;         // uuid of the open room
let selectedFolder = null;      // folder id whose contents table is shown (null = none)
let roomDetailsMap = new Map(); // room uuid -> {agents, message_count, last_message_at}
let lastId = 0;                 // highest message id rendered in currentRoom
let renderedIds = new Set();    // message ids already in the log (dedup)
let streamingBase = {};         // message id -> last full row dict (for live in-place updates)
let expandedSections = new Set(); // collapsible-row ids (thinking/debug) the user expanded
const unread = {};              // room uuid -> unread count (rooms not open)
let deferredDeletedMessageIds = new Set();
let agentsLoaded = false;
// The sidebar remembers two things separately: WHICH panel was last used
// (sidebarMode — never 'hidden') and WHETHER it is shown (sidebarVisible).
// Splitting them means hiding the sidebar — or visiting a room where the
// panel doesn't apply — never loses the panel choice.
const SIDEBAR_MODE_KEY = 'chat.sidebarMode';
const SIDEBAR_VISIBLE_KEY = 'chat.sidebarVisible';
let sidebarMode = 'members';    // 'members' | 'stats' | 'settings' | 'export'
let sidebarVisible = false;
try {
  const saved = localStorage.getItem(SIDEBAR_MODE_KEY);
  if (saved === 'members' || saved === 'stats' || saved === 'settings'
      || saved === 'export'){
    sidebarMode = saved;
    sidebarVisible = true;  // single-key era stored 'hidden' for off; a panel meant shown
  }
  const vis = localStorage.getItem(SIDEBAR_VISIBLE_KEY);
  if (vis === '1' || vis === '0') sidebarVisible = vis === '1';
} catch (e) {}
syncSidebarModeOptions();

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

// Pin the log to its newest message. Sets the scroll now AND again on the next
// frame: opening the room can resize the log a beat later (the sidebar toggling
// narrows it and rewraps text taller, images finishing load, etc.), and a
// single post-append scroll would land a line short of the true bottom.
function scrollLogToBottom(){
  log.scrollTop = log.scrollHeight;
  requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
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
async function putJSON(url, body){
  const r = await fetch(url, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(url + ' -> ' + r.status);
  return r.json();
}
// The open room's row from the tree payload (carries room_type + model_uuid).
function currentRoomObj(){
  return rooms.find(r => r.uuid === currentRoom) || null;
}
function currentRoomIsDirect(){
  const r = currentRoomObj();
  return !!(r && r.room_type === 'direct');
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
    // breaks:true — a single newline is a hard line break (chat/GFM style), so a
    // typed message renders with the same line breaks the operator entered.
    // Without it marked collapses a single newline to a space (only a blank
    // line would break), so multi-line input looked joined together.
    return DOMPurify.sanitize(marked.parse(src, { breaks: true, gfm: true }));
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
  const doCopy = (t) => {
    t = (t == null) ? '' : String(t);
    if (navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(t).then(done).catch(() => fallbackCopy(t, done));
    } else {
      fallbackCopy(t, done);
    }
  };
  // `text` may be a string, or a function returning a string/Promise (used by
  // debug-assistant rows to copy the resolved trace, not the raw pointer).
  const resolved = (typeof text === 'function') ? text() : text;
  if (resolved && typeof resolved.then === 'function') resolved.then(doCopy);
  else doCopy(resolved);
}

// Transient bottom-right toast (matches /cron, /kanban, /memory).
let chatToastTimer = null;
function chatToast(text){
  const el = document.getElementById('chat-toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(chatToastTimer);
  chatToastTimer = setTimeout(() => el.classList.remove('show'), 5000);
}
// Copy a room/folder uuid to the clipboard and confirm via the toast (not an
// in-menu "Copied" flash) — consistent with the other tree panels.
function copyIdToast(uuid, kind){
  const done = () => chatToast(kind + ' id copied: ' + uuid);
  if (navigator.clipboard && navigator.clipboard.writeText)
    navigator.clipboard.writeText(uuid).then(done).catch(() => fallbackCopy(uuid, done));
  else fallbackCopy(uuid, done);
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

// Position a fixed-position kebab menu near its anchor, clamped inside the
// viewport: preferred spot is below the anchor; if that would overflow the
// bottom edge the menu flips above it (messages at the bottom of the log,
// rooms at the bottom of the tree). Left/right are clamped with a margin.
// Unhides the menu first so its offsetWidth/Height are measurable.
function placeMenu(menu, anchorRect, alignRight){
  menu.hidden = false;
  const margin = 6;
  let left = alignRight ? (anchorRect.right - menu.offsetWidth) : anchorRect.left;
  left = Math.max(margin, Math.min(left, window.innerWidth - menu.offsetWidth - margin));
  let top = anchorRect.bottom + 4;
  if (top + menu.offsetHeight > window.innerHeight - margin){
    top = anchorRect.top - menu.offsetHeight - 4;  // flip above the anchor
  }
  top = Math.max(margin, top);
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';
}

// The message overflow (...) menu. Reuses the .room-menu styling + the global
// click/Escape dismiss handler (see below), and anchors as position:fixed under
// the kebab like the room/folder menus. Items: copy the message UUID, and (in
// direct rooms) Retry — ask the model again from this turn.
function buildMessageMenu(m){
  const uuid = m.uuid;
  const wrap = document.createElement('div');
  wrap.className = 'msg-menu-wrap';
  const kebab = document.createElement('button');
  kebab.type = 'button';
  kebab.className = 'copy-btn';
  kebab.title = 'Message actions';
  kebab.setAttribute('aria-label', 'Message actions');
  kebab.setAttribute('aria-haspopup', 'menu');
  kebab.innerHTML = LUCIDE_MORE_HORIZONTAL_SVG;
  const menu = document.createElement('div');
  menu.className = 'room-menu msg-id-menu';
  menu.setAttribute('role', 'menu');
  menu.hidden = true;
  if (currentRoomIsDirect() && !m.streaming){
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'item';
    retry.setAttribute('role', 'menuitem');
    retry.textContent = 'Retry';
    retry.title = 'Ask the model again from this turn (pick another model in Settings first if you like)';
    retry.addEventListener('click', (e) => {
      e.stopPropagation();
      menu.hidden = true;
      retryFromMessage(m);
    });
    menu.appendChild(retry);
  }
  const item = document.createElement('button');
  item.type = 'button';
  item.className = 'item';
  item.setAttribute('role', 'menuitem');
  item.textContent = 'Copy message id';
  item.addEventListener('click', (e) => {
    e.stopPropagation();
    copyText(uuid, item);  // shows "Copied" in the item briefly
    setTimeout(() => { menu.hidden = true; }, 900);
  });
  menu.appendChild(item);
  kebab.addEventListener('click', (e) => {
    e.stopPropagation();
    const willOpen = menu.hidden;
    document.querySelectorAll('.room-menu').forEach(m => { m.hidden = true; });
    if (willOpen){
      // Reparent the fixed menu to <body> before showing it. Debug rows set
      // opacity:0.8, which makes the bubble a stacking context that traps a
      // nested z-index — the menu would then paint *under* later messages no
      // matter how high its z-index. Living directly under <body> puts it in
      // the root stacking context, where z-index:1000 wins. Sweep any earlier
      // parked menu first so at most one lives in <body> at a time.
      document.querySelectorAll('body > .room-menu.msg-id-menu').forEach(m => m.remove());
      document.body.appendChild(menu);
      // Anchor at the kebab, right edges aligned so it doesn't overflow off
      // the right of the message column; clamped to the viewport.
      placeMenu(menu, kebab.getBoundingClientRect(), true);
    }
  });
  wrap.appendChild(kebab);
  wrap.appendChild(menu);
  return wrap;
}


// ---- write-proposal card (confirm / reject) ----

function renderProposalCard(m) {
  const meta = m.meta || {};
  if (!meta.write_intent) return null;
  const wrap = document.createElement('div');
  wrap.className = 'write-proposal';
  const cap = meta.capability || 'write';
  const state = meta.intent_state || 'proposed';
  if (state === 'proposed') {
    const capSpan = document.createElement('span');
    capSpan.className = 'wp-cap';
    capSpan.textContent = cap;
    wrap.appendChild(capSpan);
    const confirmBtn = document.createElement('button');
    confirmBtn.type = 'button';
    confirmBtn.className = 'wp-confirm';
    confirmBtn.textContent = 'Confirm';
    wrap.appendChild(confirmBtn);
    const rejectBtn = document.createElement('button');
    rejectBtn.type = 'button';
    rejectBtn.className = 'wp-reject';
    rejectBtn.textContent = 'Reject';
    wrap.appendChild(rejectBtn);
    if (meta.step_link) {
      const a = document.createElement('a');
      a.className = 'wp-step';
      a.setAttribute('href', meta.step_link);
      a.textContent = 'View step ↗';
      wrap.appendChild(a);
    }
    const base = '/chat/api/assistant/write-intents/' + encodeURIComponent(meta.write_intent) + '/';
    confirmBtn.addEventListener('click', () => proposalAct(wrap, base + 'confirm', cap, meta.step_link));
    rejectBtn.addEventListener('click',  () => proposalAct(wrap, base + 'reject',  cap, meta.step_link));
  } else {
    proposalFillStatus(wrap, cap, state, meta.step_link, meta.result_link);
  }
  return wrap;
}

function proposalFillStatus(wrap, cap, state, stepLink, resultLink) {
  wrap.innerHTML = '';
  const capSpan = document.createElement('span');
  capSpan.className = 'wp-cap';
  capSpan.textContent = cap;
  wrap.appendChild(capSpan);
  const label = {completed: '✓ Confirmed', rejected: '✕ Rejected',
                 failed: '⚠ Failed'}[state] || '… working';
  const stateSpan = document.createElement('span');
  stateSpan.className = 'wp-state wp-' + state;
  stateSpan.textContent = label;
  wrap.appendChild(stateSpan);
  if (stepLink) {
    const a = document.createElement('a');
    a.className = 'wp-step';
    a.setAttribute('href', stepLink);
    a.textContent = 'View step ↗';
    wrap.appendChild(a);
  }
  // A confirmed write that created something (a reminder's cron job) links to it.
  if (resultLink) {
    const a = document.createElement('a');
    a.className = 'wp-result-link';
    a.setAttribute('href', resultLink);
    a.textContent = cap === 'set_reminder' ? 'View reminder ↗' : 'View result ↗';
    wrap.appendChild(a);
  }
}

async function proposalAct(wrap, url, cap, stepLink) {
  wrap.querySelectorAll('button').forEach(b => { b.disabled = true; });
  let j = {};
  try {
    const r = await fetch(url, {method: 'POST'});
    j = await r.json();
  } catch (e) {
    j = {ok: false, text: 'network error'};
  }
  const isConfirm = url.endsWith('/confirm');
  const state = isConfirm ? (j.ok ? 'completed' : 'failed') : (j.ok ? 'rejected' : 'proposed');
  const resultLink = (j.data && j.data.link) || null;
  proposalFillStatus(wrap, cap, state, stepLink, resultLink);
  if (j.text) {
    const t = document.createElement('div');
    t.className = 'wp-result muted';
    t.textContent = j.text;
    wrap.appendChild(t);
  }
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
    // debug-assistant rows are content_type:json — the full step state, inspected
    // as JSON (no markdown rendering to hide anything).
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
  // Write-proposal card (confirm / reject) — only on messages that carry meta.write_intent.
  const proposalCard = renderProposalCard(m);
  if (proposalCard) msg.appendChild(proposalCard);
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
  // Copy = the message's stored text, uniformly for every row (debug-assistant
  // text is now the full trace, so no per-kind special-casing).
  addCopyButton(actions, m.text);
  // Edit (pencil) + delete (trash): direct rooms only — the operator can
  // rewrite or remove their own and the model's earlier turns. Agent rooms
  // never show them (the server refuses too). Editing applies to real
  // conversation turns (kind='message'); delete is on EVERY settled row —
  // notices, thinking, and debug rows are the operator's to clear out too.
  if (currentRoomIsDirect() && !m.streaming){
    if (!isDebug && m.kind === 'message'){
      const editBtn = document.createElement('button');
      editBtn.type = 'button';
      editBtn.className = 'copy-btn msg-edit-btn';
      editBtn.title = 'Edit message';
      editBtn.innerHTML = LUCIDE_PENCIL_SVG;
      editBtn.addEventListener('click', () => startEditMessage(m, msg, body, actions));
      actions.appendChild(editBtn);
    }
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'copy-btn msg-delete-btn';
    delBtn.title = 'Delete message';
    delBtn.innerHTML = LUCIDE_TRASH_SVG;
    delBtn.addEventListener('click', () => deleteMessage(m, delBtn));
    actions.appendChild(delBtn);
  }
  // Feedback row: only on agent user-facing replies. Never on human
  // messages or diagnostic rows (debug-memory / debug-query / progress /
  // thinking) — those aren't conversation outputs. Not in direct rooms
  // either: feedback rates the responder agents, and a direct chat has none
  // (the operator steers by editing/deleting messages instead).
  if (!currentRoomIsDirect() && !isDebug && m.sender_type === 'agent' && m.kind === 'message' && !m.streaming){
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
  // Overflow (...) menu, always present: "Copy message id" copies the UUID;
  // direct rooms also get "Retry" (re-ask the model from this turn).
  actions.appendChild(buildMessageMenu(m));
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

// Swap a message bubble's rendered body for an edit textarea with Save/Cancel.
// Save PUTs the new text (direct rooms only, enforced server-side) and
// re-renders the bubble from the server's updated row; Cancel re-renders the
// original. Both paths rebuild the node via upsertMessage, so no partial DOM
// state can linger.
function startEditMessage(m, msgEl, bodyEl, actionsEl){
  const room = currentRoom;
  bodyEl.style.display = 'none';
  actionsEl.style.display = 'none';
  const ta = document.createElement('textarea');
  ta.className = 'msg-edit-area';
  ta.value = m.text;
  const row = document.createElement('div');
  row.className = 'msg-edit-actions';
  const save = document.createElement('button');
  save.type = 'button';
  save.className = 'btn-save-edit';
  save.textContent = 'Save';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'btn-cancel-edit';
  cancel.textContent = 'Cancel';
  row.appendChild(save);
  row.appendChild(cancel);
  msgEl.insertBefore(ta, bodyEl);
  msgEl.insertBefore(row, bodyEl);
  ta.focus();
  cancel.addEventListener('click', () => {
    if (room === currentRoom) upsertMessage(m);
  });
  const doSave = async () => {
    const text = ta.value.trim();
    if (!text){ ta.focus(); return; }
    save.disabled = true;
    try {
      const updated = await putJSON('/chat/api/rooms/' + room + '/messages/' + m.id, { text });
      if (room === currentRoom) upsertMessage(updated);
    } catch (e) {
      save.disabled = false;
      alert('Edit failed: ' + e.message);
    }
  };
  save.addEventListener('click', doSave);
  // Enter saves, Shift+Enter inserts a newline, Escape cancels — the same
  // keys the compose box uses to send.
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !save.disabled){
      e.preventDefault();
      doSave();
    } else if (e.key === 'Escape'){
      e.stopPropagation();  // cancel the edit only, not a modal elsewhere
      if (room === currentRoom) upsertMessage(m);
    }
  });
}

// Delete a message from a direct room. The row is gone from the next turn's
// history, so the model won't see it again. The DELETE's own NOTIFY also
// reaches other open tabs (message_id 0 + the id in deleted_progress_ids),
// so removal here is just the immediate local echo.
async function deleteMessage(m, btn){
  if (!window.confirm('Delete this message? This cannot be undone.')) return;
  const room = currentRoom;
  btn.disabled = true;
  try {
    const r = await fetch('/chat/api/rooms/' + room + '/messages/' + m.id, {method: 'DELETE'});
    if (!r.ok) throw new Error('DELETE -> ' + r.status);
    if (room === currentRoom) removeDeletedMessages([m.id]);
  } catch (e) {
    btn.disabled = false;
    alert('Delete failed: ' + e.message);
  }
}

// Retry: ask the model again from this turn (direct rooms only) — e.g. after
// a timeout or a low-quality answer, typically having picked another model in
// the Settings sidebar first. The server rewinds to the turn's anchor (the
// clicked message when it's the operator's own, else the last user message
// before it) and deletes everything after it, so when later messages include
// the operator's own turns we confirm before losing them.
async function retryFromMessage(m){
  const room = currentRoom;
  // What gets deleted: rows after the clicked human message, or the clicked
  // model row and everything after it (any human rows in between are
  // impossible — the anchor is the last human turn before the model row).
  const afterId = (m.sender_type === 'human' && m.kind === 'message') ? m.id : m.id - 1;
  let following = [];
  try { following = await getJSON('/chat/api/rooms/' + room + '/messages?after=' + afterId); }
  catch (e) { alert('Retry failed: ' + e.message); return; }
  if (room !== currentRoom) return;  // room switched while loading
  const userCount = following.filter(
    r => r.sender_type === 'human' && r.kind === 'message').length;
  if (userCount > 0){
    const noun = userCount === 1 ? '1 of your own messages' : userCount + ' of your own messages';
    if (!window.confirm('Retrying from here deletes everything after this turn — '
        + 'including ' + noun + '. Continue?')) return;
  }
  try {
    const d = await postJSON('/chat/api/rooms/' + room + '/messages/' + m.id + '/retry', {});
    if (room === currentRoom && d.deleted_ids) removeDeletedMessages(d.deleted_ids);
    chatToast('Retrying — asking the model again…');
  } catch (e) {
    alert('Retry failed: ' + e.message);
  }
}

function renderRooms(){
  roomsEl.innerHTML = '';
  if (!rooms.length && !folders.length){
    const p = document.createElement('p');
    p.className = 'note';
    p.textContent = 'No rooms yet — create one above.';
    roomsEl.appendChild(p);
    return;
  }
  const rootUl = document.createElement('ul');
  childFolders(null).forEach(f => rootUl.appendChild(folderLi(f)));
  roomsInFolder(null).forEach(r => {
    const li = document.createElement('li');
    li.appendChild(roomNode(r));
    rootUl.appendChild(li);
  });
  roomsEl.appendChild(rootUl);
}

// ---- folder-contents table (right pane) ----
// Show the folder-contents table in room-main, hiding the chat. Sets the title
// to the selected folder's name, then fetches fresh room stats and renders the
// recursive subtree. Mirrors /cron's folder-details pane.
async function showFolderDetail(){
  if (selectedFolder === null){ hideFolderDetail(); return; }
  const detail = document.getElementById('folder-detail');
  document.getElementById('chat-log').hidden = true;
  document.getElementById('compose').hidden = true;
  // Hide the room title bar + clear the right sidebar so no chatroom info
  // (name, members, stats) leaks into the folder view — currentRoom is null
  // here, so renderSidebar() empties it.
  document.getElementById('room-title').hidden = true;
  renderSidebar();
  detail.hidden = false;
  const f = folderById(selectedFolder);
  document.getElementById('folder-detail-title').textContent =
    f ? ('Folder: ' + f.name) : 'Folder';
  // Refetch on each selection so counts/times stay current.
  try {
    const details = await getJSON('/chat/api/rooms/details');
    roomDetailsMap = new Map((details || []).map(d => [d.uuid, d]));
  } catch (e) {
    roomDetailsMap = new Map();
  }
  if (selectedFolder === null) return;  // user navigated away while fetching
  renderFolderDetailRows();
}

// Restore the chat view (called when a room is opened).
function hideFolderDetail(){
  document.getElementById('folder-detail').hidden = true;
  document.getElementById('chat-log').hidden = false;
  document.getElementById('compose').hidden = false;
  document.getElementById('room-title').hidden = false;
}

// Render the selected folder's recursive subtree as depth-indented rows.
function renderFolderDetailRows(){
  const tbody = document.getElementById('folder-detail-rows');
  tbody.innerHTML = '';
  const rootFolders = childFolders(selectedFolder);
  const rootRooms = roomsInFolder(selectedFolder);
  if (!rootFolders.length && !rootRooms.length){
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 5;
    td.className = 'fd-empty';
    td.textContent = 'empty folder';
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  // Pre-order walk: each folder's rooms, then recurse into its subfolders.
  const walk = (folderId, depth) => {
    childFolders(folderId).forEach(sub => {
      tbody.appendChild(folderDetailFolderRow(sub, depth));
      walk(sub.id, depth + 1);
    });
    roomsInFolder(folderId).forEach(r => {
      tbody.appendChild(folderDetailRoomRow(r, depth));
    });
  };
  walk(selectedFolder, 0);
}

function fdIndent(depth){ return '\\u00A0\\u00A0'.repeat(depth); }

function fdDetailsLink(onClick){
  const a = document.createElement('a');
  a.className = 'fd-details';
  a.textContent = 'Details';
  a.addEventListener('click', onClick);
  return a;
}

// A subfolder row: name (indented) + Details link that drills into it.
function folderDetailFolderRow(f, depth){
  const tr = document.createElement('tr');
  const nameTd = document.createElement('td');
  nameTd.className = 'fd-name';
  const indent = document.createElement('span');
  indent.textContent = fdIndent(depth);
  const ic = document.createElement('span');
  ic.className = 'fd-icon';
  ic.innerHTML = CHAT_ICON_FOLDER;
  nameTd.appendChild(indent);
  nameTd.appendChild(ic);
  nameTd.appendChild(document.createTextNode(' ' + f.name));
  tr.appendChild(nameTd);
  tr.appendChild(document.createElement('td'));               // agents (blank)
  const mTd = document.createElement('td'); mTd.className = 'fd-num'; tr.appendChild(mTd);  // messages (blank)
  tr.appendChild(document.createElement('td'));               // last message (blank)
  const actTd = document.createElement('td');
  actTd.appendChild(fdDetailsLink(() => {
    selectedFolder = f.id;
    currentRoom = null;
    renderRooms();
    showFolderDetail();
    chatSyncUrl();
  }));
  tr.appendChild(actTd);
  return tr;
}

// A room row: name (indented) + agents + message count + last message time,
// plus a Details link that opens the chatroom (reuses selectRoom).
function folderDetailRoomRow(r, depth){
  const d = roomDetailsMap.get(r.uuid) || {};
  const tr = document.createElement('tr');
  const nameTd = document.createElement('td');
  nameTd.className = 'fd-name';
  nameTd.textContent = fdIndent(depth) + r.name;
  tr.appendChild(nameTd);
  const agentsTd = document.createElement('td');
  agentsTd.textContent = (d.agents || []).join(', ');
  tr.appendChild(agentsTd);
  const mTd = document.createElement('td');
  mTd.className = 'fd-num';
  mTd.textContent = (d.message_count != null) ? d.message_count : '';
  tr.appendChild(mTd);
  const lastTd = document.createElement('td');
  lastTd.textContent = d.last_message_at ? d.last_message_at : '—';
  tr.appendChild(lastTd);
  const actTd = document.createElement('td');
  actTd.appendChild(fdDetailsLink(() => { selectRoom(r.uuid); }));
  tr.appendChild(actTd);
  return tr;
}

// A folder row: the folder icon flips open when expanded and the folder has
// children. Click toggles expand/collapse. Ported from cronFolderLi.
function folderLi(f){
  const li = document.createElement('li');
  const kids = childFolders(f.id);
  const kidRooms = roomsInFolder(f.id);
  const hasKids = (kids.length + kidRooms.length) > 0;
  const expanded = isExpanded(f.id);
  // A real anchor so CMD/Ctrl/middle click opens the folder view in a new
  // tab; a plain click is intercepted below and selects/toggles in-page.
  const node = document.createElement('a');
  node.className = 'chat-node' + (selectedFolder === f.id ? ' sel' : '');
  node.href = '/chat?id=' + encodeURIComponent(f.id);
  const icon = document.createElement('span');
  icon.className = 'chat-ficon';
  icon.innerHTML = (expanded && hasKids) ? CHAT_ICON_FOLDER_OPEN : CHAT_ICON_FOLDER;
  const label = document.createElement('span');
  label.className = 'chat-folder-label';
  label.textContent = f.name;
  node.appendChild(icon);
  node.appendChild(label);
  node.title = f.name;
  node.addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
    e.preventDefault();
    // First click selects the folder (shows its contents table); clicking the
    // already-selected folder toggles its expand/collapse (mirrors /cron).
    const wasSelected = (selectedFolder === f.id);
    if (wasSelected){
      expandedFolders[f.id] = !isExpanded(f.id);
      saveExpandState();
    } else {
      selectedFolder = f.id;
      currentRoom = null;  // a folder and a room are never selected at once
    }
    renderRooms();
    showFolderDetail();
    chatSyncUrl();
  });
  makeDraggable(node, 'folder', f.id);
  makeFolderDrop(node, f.id);
  node.appendChild(buildFolderMenu(f.id));
  li.appendChild(node);
  if (expanded && hasKids){
    const ul = document.createElement('ul');
    kids.forEach(c => ul.appendChild(folderLi(c)));
    kidRooms.forEach(r => { const rli = document.createElement('li'); rli.appendChild(roomNode(r)); ul.appendChild(rli); });
    li.appendChild(ul);
  }
  return li;
}

// A room row — keeps the existing .room-row/.room markup (name, sub, unread,
// kebab) so selection/menus look identical to today, wrapped for drag-drop.
function roomNode(r){
  const isActive = r.uuid === currentRoom;
  const row = document.createElement('div');
  row.className = 'room-row' + (isActive ? ' active' : '');
  // A real anchor so CMD/Ctrl/middle click opens the chat in a new tab; a
  // plain click is intercepted below and selects the room in-page instead.
  const btn = document.createElement('a');
  btn.className = 'room' + (isActive ? ' active' : '');
  btn.href = '/chat?id=' + encodeURIComponent(r.uuid);
  btn.draggable = false;  // the row is the drag source, not the link's URL
  btn.dataset.room = r.uuid;
  const name = document.createElement('span');
  name.className = 'room-name';
  name.textContent = r.name;
  btn.appendChild(name);
  const n = unread[r.uuid] || 0;
  if (n > 0){
    const dot = document.createElement('span');
    dot.className = 'unread';
    dot.textContent = n;
    btn.appendChild(dot);
  }
  btn.addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
    e.preventDefault();
    selectRoom(r.uuid);
  });
  row.appendChild(btn);
  if (isActive) row.appendChild(buildRoomMenu(r.uuid));
  makeDraggable(row, 'room', r.uuid);
  makeRoomDrop(row, r.uuid);
  return row;
}

// ---- drag & drop (ported from static/cron.js) ----
function folderInSubtree(candidateId, rootId){
  let cur = folderById(candidateId);
  while (cur){
    if (cur.id === rootId) return true;
    cur = cur.parentId ? folderById(cur.parentId) : null;
  }
  return false;
}
function moveFolder(folderId, targetParentId, atStart){
  targetParentId = targetParentId || null;
  if (folderId === targetParentId) return;
  if (targetParentId && folderInSubtree(targetParentId, folderId)) return;  // no cycles
  const f = folderById(folderId);
  if (!f) return;
  f.parentId = targetParentId;
  folders = folders.filter(x => x.id !== folderId);
  if (atStart){
    const i = folders.findIndex(x => (x.parentId || null) === targetParentId);
    if (i < 0) folders.push(f); else folders.splice(i, 0, f);
  } else {
    let at = folders.length;
    for (let i = folders.length - 1; i >= 0; i--){
      if ((folders[i].parentId || null) === targetParentId){ at = i + 1; break; }
    }
    folders.splice(at, 0, f);
  }
  saveTree();
}
function moveFolderBeside(folderId, targetFolderId, after){
  if (folderId === targetFolderId) return;
  const target = folderById(targetFolderId);
  if (!target) return;
  const newParent = target.parentId || null;
  if (newParent && folderInSubtree(newParent, folderId)) return;  // no cycles
  const f = folderById(folderId);
  if (!f) return;
  f.parentId = newParent;
  folders = folders.filter(x => x.id !== folderId);
  const ti = folders.findIndex(x => x.id === targetFolderId);
  if (ti < 0) folders.push(f);
  else folders.splice(after ? ti + 1 : ti, 0, f);
  saveTree();
}
function moveRoom(roomUuid, targetFolderId, beforeRoomUuid){
  targetFolderId = targetFolderId || null;
  const idx = rooms.findIndex(r => r.uuid === roomUuid);
  if (idx < 0) return;
  const room = rooms.splice(idx, 1)[0];
  room.folderId = targetFolderId;
  let insertAt = beforeRoomUuid ? rooms.findIndex(r => r.uuid === beforeRoomUuid) : -1;
  if (insertAt < 0){
    insertAt = rooms.length;
    for (let i = rooms.length - 1; i >= 0; i--){
      if ((rooms[i].folderId || null) === targetFolderId){ insertAt = i + 1; break; }
    }
  }
  rooms.splice(insertAt, 0, room);
  saveTree();
}
function makeDraggable(el, type, id){
  el.draggable = true;
  el.addEventListener('dragstart', e => {
    dragNode = {type: type, id: id};
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', id);  // Firefox needs data to start a drag
    el.classList.add('chat-dragging');
    document.querySelector('.rooms').classList.add('dragging-on');  // reveal root drop zone
    e.stopPropagation();
  });
  el.addEventListener('dragend', () => {
    dragNode = null;
    document.querySelector('.rooms').classList.remove('dragging-on');
    renderRooms();
  });
}
function dropInto(folderId, atStart){
  if (!dragNode) return;
  const dragged = dragNode;
  if (dragged.type === 'room'){
    let beforeUuid = null;
    if (atStart){
      const first = rooms.find(r =>
        (r.folderId || null) === (folderId || null) && r.uuid !== dragged.id);
      beforeUuid = first ? first.uuid : null;
    }
    moveRoom(dragged.id, folderId, beforeUuid);
  } else {
    moveFolder(dragged.id, folderId, atStart);
  }
  if (folderId){ expandedFolders[folderId] = true; saveExpandState(); }
  dragNode = null;
  renderRooms();
}
function makeFolderDrop(node, folderId){
  const zoneOf = e => {
    if (dragNode && dragNode.type === 'room') return 'into';
    const r = node.getBoundingClientRect();
    const y = e.clientY - r.top;
    if (y < r.height / 3) return 'before';
    if (y > r.height * 2 / 3) return 'after';
    return 'into';
  };
  const okFor = z => {
    if (!dragNode) return false;
    if (dragNode.type === 'room') return z === 'into';
    if (folderId === dragNode.id) return false;
    if (z === 'into') return !folderInSubtree(folderId, dragNode.id);
    const t = folderById(folderId);
    const np = t ? (t.parentId || null) : null;
    return !(np && folderInSubtree(np, dragNode.id));
  };
  const clear = () => node.classList.remove('chat-drop-before', 'chat-drop-after', 'chat-drop-target');
  node.addEventListener('dragover', e => {
    if (!dragNode) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    node.classList.toggle('chat-drop-before', z === 'before');
    node.classList.toggle('chat-drop-after', z === 'after');
    node.classList.toggle('chat-drop-target', z === 'into');
  });
  node.addEventListener('dragleave', clear);
  node.addEventListener('drop', e => {
    if (!dragNode) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    clear();
    if (z === 'into'){
      dropInto(folderId, false);
    } else {
      moveFolderBeside(dragNode.id, folderId, z === 'after');
      dragNode = null;
      renderRooms();
    }
  });
}
function makeRoomDrop(node, roomUuid){
  const isAfter = e => {
    const r = node.getBoundingClientRect();
    return (e.clientY - r.top) > r.height / 2;
  };
  node.addEventListener('dragover', e => {
    if (!dragNode) return;
    e.preventDefault(); e.stopPropagation();
    e.dataTransfer.dropEffect = 'move';
    const after = isAfter(e);
    node.classList.toggle('chat-drop-after', after);
    node.classList.toggle('chat-drop-before', !after);
  });
  node.addEventListener('dragleave', () => node.classList.remove('chat-drop-before', 'chat-drop-after'));
  node.addEventListener('drop', e => {
    if (!dragNode) return;
    e.preventDefault(); e.stopPropagation();
    const after = isAfter(e);
    node.classList.remove('chat-drop-before', 'chat-drop-after');
    dropOnRoom(roomUuid, after);
  });
}
function dropOnRoom(targetUuid, after){
  if (!dragNode) return;
  if (dragNode.type === 'room' && dragNode.id === targetUuid) return;  // onto itself
  const dragged = dragNode;
  const target = rooms.find(r => r.uuid === targetUuid);
  const targetFolder = target ? (target.folderId || null) : null;
  if (dragged.type === 'room'){
    let beforeUuid = targetUuid;
    if (after){
      const ti = rooms.findIndex(r => r.uuid === targetUuid);
      beforeUuid = (ti + 1 < rooms.length) ? rooms[ti + 1].uuid : null;
    }
    if (beforeUuid === dragged.id) beforeUuid = null;
    moveRoom(dragged.id, targetFolder, beforeUuid);
  } else {
    moveFolder(dragged.id, targetFolder);
  }
  dragNode = null;
  renderRooms();
}
function wireRootDrop(el, atStart){
  el.addEventListener('dragover', e => {
    if (dragNode){ e.preventDefault(); e.stopPropagation(); e.dataTransfer.dropEffect = 'move'; el.classList.add('over'); }
  });
  el.addEventListener('dragleave', () => el.classList.remove('over'));
  el.addEventListener('drop', e => {
    if (dragNode){ e.preventDefault(); e.stopPropagation(); el.classList.remove('over'); dropInto(null, atStart); }
  });
}

// ---- persistence: debounced PUT of the whole tree ----
let saveTimer = null;
function saveTree(){
  renderRooms();
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(saveTreePush, 300);
}
async function saveTreePush(){
  if (!treeVersion){ await loadRooms(currentRoom); return; }  // no token -> re-hydrate, never blind-PUT
  const body = {
    folders: folders.map(f => ({id: f.id, name: f.name, parentId: f.parentId || null})),
    rooms: rooms.map(r => ({uuid: r.uuid, folderId: r.folderId || null})),
    version: treeVersion,
  };
  try {
    const resp = await fetch('/chat/api/tree', {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (resp.status === 409){ await loadRooms(currentRoom); return; }  // stale -> re-hydrate
    if (!resp.ok) throw new Error(data.error || ('PUT /chat/api/tree -> ' + resp.status));
    treeVersion = data.version || treeVersion;
  } catch (e) {
    await loadRooms(currentRoom);  // recover to server truth on any error
  }
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
  [['Copy room id', ''], ['Delete', 'danger']].forEach(([label, mod]) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'item' + (mod ? ' ' + mod : '');
    item.setAttribute('role', 'menuitem');
    item.textContent = label;
    item.addEventListener('click', (e) => {
      e.stopPropagation();
      menu.hidden = true;
      if (label === 'Copy room id') copyIdToast(roomUuid, 'Room');
      else if (label === 'Delete') deleteRoom(roomUuid);
    });
    menu.appendChild(item);
  });
  kebab.addEventListener('click', (e) => {
    e.stopPropagation();  // don't let the row's click re-select / dismiss
    const willOpen = menu.hidden;
    document.querySelectorAll('.room-menu').forEach(m => { m.hidden = true; });
    if (willOpen){
      // Anchor the fixed menu at the kebab, left edges aligned; viewport-clamped.
      placeMenu(menu, kebab.getBoundingClientRect(), false);
    }
  });
  wrap.appendChild(kebab);
  wrap.appendChild(menu);
  return wrap;
}

// Folder kebab: Rename + Delete. The wrap is laid out (display:flex) but its
// visibility is governed by CSS — shown only when the folder node is selected
// (.chat-node.sel) or hovered, mirroring the rooms' active-only kebab.
function buildFolderMenu(folderId){
  const wrap = document.createElement('div');
  wrap.className = 'room-actions';
  wrap.style.display = 'flex';  // keep it laid out; CSS controls visibility
  const kebab = document.createElement('button');
  kebab.type = 'button';
  kebab.className = 'room-kebab';
  kebab.setAttribute('aria-label', 'Folder actions');
  const menu = document.createElement('div');
  menu.className = 'room-menu';
  menu.setAttribute('role', 'menu');
  menu.hidden = true;
  [['Rename', ''], ['Copy folder id', ''], ['Delete', 'danger']].forEach(([label, mod]) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'item' + (mod ? ' ' + mod : '');
    item.setAttribute('role', 'menuitem');
    item.textContent = label;
    item.addEventListener('click', (e) => {
      e.stopPropagation();
      e.preventDefault();  // the menu sits inside the folder's anchor — never follow it
      menu.hidden = true;
      if (label === 'Copy folder id') copyIdToast(folderId, 'Folder');
      else if (label === 'Delete') confirmDeleteFolder(folderId);
      else if (label === 'Rename') renameFolder(folderId);
    });
    menu.appendChild(item);
  });
  kebab.addEventListener('click', (e) => {
    e.stopPropagation();
    e.preventDefault();  // the kebab sits inside the folder's anchor — never follow it
    const willOpen = menu.hidden;
    document.querySelectorAll('.room-menu').forEach(m => { m.hidden = true; });
    if (willOpen){
      placeMenu(menu, kebab.getBoundingClientRect(), false);
    }
  });
  wrap.appendChild(kebab);
  wrap.appendChild(menu);
  return wrap;
}

// Inline-rename a folder: reuse the folder-create modal in "rename" mode.
function renameFolder(folderId){
  const f = folderById(folderId);
  if (!f) return;
  openFolderModal({mode: 'rename', folderId: folderId, current: f.name});
}

// ---- folder create / rename modal ----
let folderModalState = null;  // {mode:'create'|'rename', folderId?, parentId?, current?}
function openFolderModal(opts){
  folderModalState = opts || {mode: 'create', parentId: null};
  document.getElementById('chat-folder-title').textContent =
    folderModalState.mode === 'rename' ? 'Rename folder' : 'New folder';
  const input = document.getElementById('chat-folder-input');
  input.value = folderModalState.current || '';
  document.getElementById('chat-folder-create').textContent =
    folderModalState.mode === 'rename' ? 'Rename' : 'Create';
  document.getElementById('chat-folder-create').disabled = !input.value.trim();
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('chat-folder-modal').hidden = false;
  input.focus();
  input.select();
}
function closeFolderModal(){
  document.getElementById('chat-folder-modal').hidden = true;
  document.getElementById('ui-modal-backdrop').hidden = true;
  folderModalState = null;
}
async function confirmFolderModal(){
  const name = document.getElementById('chat-folder-input').value.trim();
  if (!name || !folderModalState) return;
  if (folderModalState.mode === 'rename'){
    const f = folderById(folderModalState.folderId);
    if (f){ f.name = name; saveTree(); }   // rename persists via the tree PUT
    closeFolderModal();
    return;
  }
  // create: POST, then re-hydrate so the new folder gets a server position.
  try {
    const resp = await fetch('/chat/api/folders', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name}),
    });
    if (!resp.ok) throw new Error('POST /chat/api/folders -> ' + resp.status);
  } catch (e) { alert(e); return; }
  closeFolderModal();
  await loadRooms(currentRoom);
}
document.getElementById('new-folder-btn').addEventListener('click', () => openFolderModal({mode: 'create', parentId: null}));
document.getElementById('chat-folder-cancel').addEventListener('click', closeFolderModal);
document.getElementById('chat-folder-create').addEventListener('click', confirmFolderModal);
document.getElementById('chat-folder-input').addEventListener('input', e => {
  document.getElementById('chat-folder-create').disabled = !e.target.value.trim();
});
document.getElementById('chat-folder-input').addEventListener('keydown', e => {
  if (e.key === 'Enter'){ e.preventDefault(); confirmFolderModal(); }
});

// ---- type-to-confirm destructive delete (folder or room) ----
let deleteModalState = null;  // {kind:'folder'|'room', id, name}
function fmtCount(n){ return Number(n).toLocaleString(); }
function openDeleteModal(state, message, confirmName){
  deleteModalState = state;
  document.getElementById('chat-delete-title').textContent =
    state.kind === 'folder' ? 'Delete folder' : 'Delete room';
  document.getElementById('chat-delete-msg').textContent = message;
  document.getElementById('chat-delete-name').textContent = confirmName;
  const input = document.getElementById('chat-delete-input');
  input.value = '';
  const confirmBtn = document.getElementById('chat-delete-confirm');
  confirmBtn.disabled = true;
  input.oninput = () => { confirmBtn.disabled = (input.value !== confirmName); };
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('chat-delete-modal').hidden = false;
  input.focus();
}
function closeDeleteModal(){
  document.getElementById('chat-delete-modal').hidden = true;
  document.getElementById('ui-modal-backdrop').hidden = true;
  deleteModalState = null;
}
async function confirmDeleteFolder(folderId){
  const f = folderById(folderId);
  if (!f) return;
  let preview;
  try {
    preview = await getJSON('/chat/api/folders/' + folderId + '/delete-preview');
  } catch (e) { alert(e); return; }
  const msg = 'Are you sure you want to delete ' +
    fmtCount(preview.room_count) + (preview.room_count === 1 ? ' chatroom' : ' chatrooms') +
    ' containing ' + fmtCount(preview.message_count) +
    (preview.message_count === 1 ? ' message' : ' messages') + '? This cannot be undone.';
  openDeleteModal({kind: 'folder', id: folderId, name: f.name}, msg, f.name);
}
async function deleteRoom(uuid){
  const room = rooms.find(r => r.uuid === uuid);
  if (!room) return;
  let preview;
  try {
    preview = await getJSON('/chat/api/rooms/' + uuid + '/delete-preview');
  } catch (e) { alert(e); return; }
  const msg = 'Are you sure you want to delete # ' + preview.room_name + ' containing ' +
    fmtCount(preview.message_count) +
    (preview.message_count === 1 ? ' message' : ' messages') + '? This cannot be undone.';
  openDeleteModal({kind: 'room', id: uuid, name: preview.room_name}, msg, preview.room_name);
}
async function performConfirmedDelete(){
  if (!deleteModalState) return;
  const {kind, id} = deleteModalState;
  const url = kind === 'folder' ? '/chat/api/folders/' + id : '/chat/api/rooms/' + id;
  try {
    const r = await fetch(url, {method: 'DELETE'});
    if (!r.ok) throw new Error('DELETE ' + url + ' -> ' + r.status);
  } catch (e) { alert(e); return; }
  if (kind === 'room') delete unread[id];
  closeDeleteModal();
  // Was the open room removed? Directly (room delete) or because its folder
  // (which may have held it) was deleted. Clear currentRoom BEFORE re-hydrating
  // so loadRooms doesn't try to reselect the now-deleted room — it auto-selects
  // rooms[0] in a single pass.
  const hadOpenRoom = currentRoom;
  if (kind === 'room' && currentRoom === id) currentRoom = null;
  if (kind === 'folder' && selectedFolder === id) selectedFolder = null;
  await loadRooms(currentRoom);
  chatSyncUrl();  // reflect the post-delete selection (clears a stale ?id=)
  // If the open room is gone after re-hydration (room delete, or a folder
  // delete that contained it) and nothing got auto-selected, clear the pane.
  if (hadOpenRoom && !rooms.some(r => r.uuid === hadOpenRoom) && !currentRoom){
    titleNameEl.textContent = '';
    log.innerHTML = '';
    renderSidebar();
  }
}
document.getElementById('chat-delete-cancel').addEventListener('click', closeDeleteModal);
document.getElementById('chat-delete-confirm').addEventListener('click', performConfirmedDelete);

// Dismiss any open room overflow menu on an outside click or Escape.
document.addEventListener('click', () => {
  document.querySelectorAll('.room-menu').forEach(m => { m.hidden = true; });
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') document.querySelectorAll('.room-menu').forEach(m => { m.hidden = true; });
});

// The id of the node currently inspected (room > folder > none). A single
// ?id=<uuid> mirrors it (like /cron's ?id=), addressing either a room or a
// folder — uuids are globally unique across kinds.
function chatCurrentSelectionId(){
  if (currentRoom) return currentRoom;
  if (selectedFolder) return selectedFolder;
  return null;
}
function chatSyncUrl(){
  const url = new URL(window.location);
  url.searchParams.delete('room');  // migrate off the old per-kind param
  url.searchParams.delete('msg');   // a one-shot deep-link anchor, already consumed
  const id = chatCurrentSelectionId();
  if (id) url.searchParams.set('id', id); else url.searchParams.delete('id');
  history.replaceState(null, '', url);
}

async function selectRoom(uuid, scrollMsgId){
  currentRoom = uuid;
  selectedFolder = null;  // opening a room clears any folder selection
  hideFolderDetail();     // swap the right pane back to the chat view
  unread[uuid] = 0;
  lastId = 0;
  renderedIds = new Set();
  streamingBase = {};
  expandedSections = new Set();
  chatSyncUrl();          // mirror the open room into ?id= so a reload reopens it
  renderRooms();
  const room = rooms.find(r => r.uuid === uuid);
  titleNameEl.textContent = room ? room.name : '';
  // A direct room with no model — and no global default to fall back to —
  // can't reply, so surface its Settings panel for this visit (not persisted
  // to localStorage) so a fresh room is immediately configurable.
  if (room && room.room_type === 'direct' && !room.model_uuid && !chatDefaultModel
      && !sidebarVisible){
    sidebarVisible = true;
    sidebarMode = 'settings';
  }
  syncSidebarModeOptions();
  log.innerHTML = '';
  input.focus();
  const msgs = await getJSON('/chat/api/rooms/' + uuid + '/messages?after=0');
  if (uuid !== currentRoom) return;  // room switched while loading
  msgs.forEach(appendMessage);
  renderSidebar();  // members/stats reflect the now-open room — resizes the log,
                    // so toggle it BEFORE scrolling or we land a line short.
  scrollLogToBottom();
  if (scrollMsgId) scrollToMessage(scrollMsgId);
}

// Deep-link scroll: bring a specific message (by its int id, the DOM's
// data-message-id) into view and flash it. Runs after the open's
// scroll-to-bottom settles (and a sidebar resize), so it wins the final scroll.
function scrollToMessage(msgId){
  let tries = 0;
  const go = () => {
    const el = log.querySelector('[data-message-id="' + msgId + '"]');
    if (el){
      el.scrollIntoView({behavior: 'smooth', block: 'center'});
      el.classList.add('msg-highlight');
      setTimeout(() => el.classList.remove('msg-highlight'), 2600);
    } else if (tries++ < 5){
      setTimeout(go, 80);  // the message may still be rendering/laying out
    }
  };
  setTimeout(go, 80);
}

async function fetchNew(uuid){
  if (uuid !== currentRoom) return;
  const msgs = await getJSON('/chat/api/rooms/' + uuid + '/messages?after=' + lastId);
  if (uuid !== currentRoom || !msgs.length) return;  // re-check after await
  msgs.forEach(appendMessage);
  scrollLogToBottom();
  if (activeSidebarMode() === 'stats') renderStats();  // keep the live message count fresh
}

async function loadRooms(selectUuid, scrollMsgId){
  const tree = await getJSON('/chat/api/tree');
  folders = (tree && tree.folders) || [];
  rooms = (tree && tree.rooms) || [];
  treeVersion = (tree && tree.version) || null;
  chatDefaultModel = (tree && tree.default_model_uuid) || null;
  renderRooms();
  // A deep-linked id may name a FOLDER (?id=<folder>) — select its contents
  // table instead of a room.
  if (selectUuid && folders.some(f => f.id === selectUuid)){
    selectedFolder = selectUuid;
    currentRoom = null;
    renderRooms();
    showFolderDetail();
    chatSyncUrl();
    return;
  }
  let target = selectUuid || currentRoom;
  // Fall back to the top-most room in the hierarchy when nothing is requested,
  // or when the requested one is missing (e.g. a stale ?id= for a deleted room).
  if (!target || !rooms.some(r => r.uuid === target)){
    target = firstRoomInTree(null);
  }
  if (target) await selectRoom(target, scrollMsgId);
  else chatSyncUrl();  // nothing to open (no rooms) — clear a stale ?id=
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

// ---- room rename modal (docs/ui-modal-rename.md) ----
// The title bar shows the room name as a click-to-rename control; all editing
// happens in the modal, so a typed-but-unconfirmed name can't be silently lost.
let chatRenameOriginal = null;   // the stored name at open (null = modal closed)
function openChatRenameModal(){
  const room = currentRoomObj();
  if (!room) return;
  chatRenameOriginal = room.name;
  const input = document.getElementById('chat-rename-input');
  input.value = room.name;
  syncChatRenameConfirm();
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('chat-rename-modal').hidden = false;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}
function closeChatRenameModal(){
  document.getElementById('chat-rename-modal').hidden = true;
  document.getElementById('ui-modal-backdrop').hidden = true;
  chatRenameOriginal = null;
}
// Rename is enabled only for a non-empty name that actually differs.
function syncChatRenameConfirm(){
  const v = document.getElementById('chat-rename-input').value.trim();
  document.getElementById('chat-rename-confirm').disabled =
    v === '' || chatRenameOriginal === null || v === chatRenameOriginal;
}
async function confirmChatRenameModal(){
  const room = currentRoomObj();
  const v = document.getElementById('chat-rename-input').value.trim();
  if (!room || !v || v === chatRenameOriginal) return;
  try {
    await postJSON('/chat/api/rooms/' + room.uuid + '/rename', { name: v });
  } catch (e) {
    alert('Rename failed: ' + e.message);
    return;
  }
  room.name = v;
  closeChatRenameModal();
  renderRooms();  // reflect the new name in the left panel
  titleNameEl.textContent = v;
  chatToast('Renamed to “' + v + '”');
}
titleNameEl.addEventListener('click', openChatRenameModal);
document.getElementById('chat-rename-cancel').addEventListener('click', closeChatRenameModal);
document.getElementById('chat-rename-confirm').addEventListener('click', confirmChatRenameModal);
document.getElementById('chat-rename-input').addEventListener('input', syncChatRenameConfirm);
document.getElementById('chat-rename-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !document.getElementById('chat-rename-confirm').disabled){
    e.preventDefault(); confirmChatRenameModal();
  }
});

// Members lists agents, so it's meaningless in a direct LLM room; Settings
// (model picker + system prompt) only exists for direct rooms. Rather than
// forgetting the remembered mode in a room where it doesn't apply, map it to
// its counterpart (Members↔Settings); Stats/Export are shared and carry over
// as-is. So navigating between room types never hides the sidebar.
function effectiveSidebarMode(){
  const direct = currentRoomIsDirect();
  if (direct && sidebarMode === 'members') return 'settings';
  if (!direct && sidebarMode === 'settings') return 'members';
  return sidebarMode;
}
// What the right pane is actually showing: the mapped mode, or 'hidden' when
// the sidebar is toggled off or no room is open.
function activeSidebarMode(){
  return (sidebarVisible && currentRoom) ? effectiveSidebarMode() : 'hidden';
}
function persistSidebarPrefs(){
  try {
    localStorage.setItem(SIDEBAR_MODE_KEY, sidebarMode);
    localStorage.setItem(SIDEBAR_VISIBLE_KEY, sidebarVisible ? '1' : '0');
  } catch (e) {}
}
// Keep the select honest for the open room: hide the option that doesn't
// apply to its type, and point the value at what is actually displayed.
function syncSidebarModeOptions(){
  const direct = currentRoomIsDirect();
  const membersOpt = sidebarModeSel.querySelector('option[value="members"]');
  const settingsOpt = sidebarModeSel.querySelector('option[value="settings"]');
  membersOpt.hidden = membersOpt.disabled = direct;
  settingsOpt.hidden = settingsOpt.disabled = !direct;
  sidebarModeSel.value = activeSidebarMode();
}

// Right sidebar: hidden / members / stats / settings / export.
async function renderSidebar(){
  const mode = activeSidebarMode();
  if (mode === 'hidden'){
    splitEl.classList.remove('sidebar-open');
    sidebarEl.innerHTML = '';
    return;
  }
  splitEl.classList.add('sidebar-open');
  if (mode === 'members') await renderMembers();
  else if (mode === 'stats') renderStats();
  else if (mode === 'settings') await renderDirectSettings();
  else if (mode === 'export') renderExport();
}

// Settings panel for a direct room: which model it talks to + its system
// prompt. Both apply from the next turn on. Fetched on open (user activity,
// not polling). For agents rooms it just explains itself.
async function renderDirectSettings(){
  const room = currentRoom;
  sidebarEl.innerHTML = '';
  const h = document.createElement('h3');
  h.className = 'sidebar-title';
  h.textContent = 'Settings';
  sidebarEl.appendChild(h);
  if (!currentRoomIsDirect()){
    const note = document.createElement('p');
    note.className = 'ds-note';
    note.textContent = 'Settings are only available in direct LLM chats.';
    sidebarEl.appendChild(note);
    return;
  }
  let settings, models;
  try {
    [settings, models] = await Promise.all([
      getJSON('/chat/api/rooms/' + room + '/settings'),
      getJSON('/chat/api/models'),
    ]);
  } catch (_) { return; }
  if (room !== currentRoom || activeSidebarMode() !== 'settings') return;  // changed while loading
  const modelLabel = document.createElement('span');
  modelLabel.className = 'ds-label';
  modelLabel.textContent = 'Model';
  sidebarEl.appendChild(modelLabel);
  const sel = document.createElement('select');
  sel.className = 'ds-model';
  const none = document.createElement('option');
  none.value = '';
  // No per-room model means the global default (chat.default_model on
  // /settings) answers; say which one so the blank state isn't alarming.
  const dfltModel = settings.default_model_uuid
    ? models.find(mm => mm.uuid === settings.default_model_uuid) : null;
  none.textContent = dfltModel ? '(default — ' + dfltModel.label + ')' : '(no model)';
  sel.appendChild(none);
  models.forEach(mm => {
    const opt = document.createElement('option');
    opt.value = mm.uuid;
    opt.textContent = mm.label + (mm.available ? '' : ' (unavailable)');
    sel.appendChild(opt);
  });
  sel.value = settings.model_uuid || '';
  sidebarEl.appendChild(sel);
  // Per-room reply timeout. Empty = the model config's request_timeout (or
  // 60s) — raise it for long conversations where prompt processing alone can
  // exceed the default.
  const timeoutLabel = document.createElement('span');
  timeoutLabel.className = 'ds-label';
  timeoutLabel.textContent = 'Request timeout (seconds)';
  sidebarEl.appendChild(timeoutLabel);
  const timeoutInput = document.createElement('input');
  timeoutInput.type = 'number';
  timeoutInput.className = 'ds-timeout';
  timeoutInput.min = '1';
  timeoutInput.step = '1';
  timeoutInput.placeholder = 'default (model config, else 60)';
  if (settings.request_timeout) timeoutInput.value = settings.request_timeout;
  sidebarEl.appendChild(timeoutInput);
  const promptLabel = document.createElement('span');
  promptLabel.className = 'ds-label';
  promptLabel.textContent = 'System prompt';
  sidebarEl.appendChild(promptLabel);
  // The prompt is either a LINKED stored version (picked from the /prompt
  // tree; its content is resolved fresh each turn) or the room's own free
  // text. Linking keeps the free text around, so Unlink restores it.
  const modeRow = document.createElement('div');
  modeRow.className = 'ds-prompt-mode';
  sidebarEl.appendChild(modeRow);
  const ta = document.createElement('textarea');
  ta.className = 'ds-prompt';
  sidebarEl.appendChild(ta);
  let customText = settings.system_prompt || '';
  let linked = settings.prompt_uuid
    ? {uuid: settings.prompt_uuid, name: settings.prompt_name || '',
       exists: settings.prompt_exists !== false}
    : null;
  ta.addEventListener('input', () => { if (!linked) customText = ta.value; });
  // Read-only preview of the linked version's current content (what the next
  // reply will actually send; the turn itself resolves server-side).
  async function showLinkedPreview(uuid){
    ta.value = '';
    ta.placeholder = 'Loading stored prompt…';
    let d = null;
    try { d = await getJSON('/prompt/api/prompts/' + uuid); } catch (_) { return; }
    if (room !== currentRoom || activeSidebarMode() !== 'settings') return;
    if (linked && linked.uuid === uuid) ta.value = (d && d.content) || '';
  }
  function renderPromptSource(){
    modeRow.innerHTML = '';
    if (linked){
      const a = document.createElement('a');
      a.href = '/prompt?id=' + encodeURIComponent(linked.uuid);
      a.target = '_blank';
      if (linked.exists){
        a.textContent = linked.name || 'stored prompt';
        a.title = 'Open this prompt on the Prompt page';
      } else {
        a.textContent = '(deleted prompt)';
        a.className = 'gone';
        a.title = 'The linked version was deleted on the Prompt page; no system message is sent.';
      }
      modeRow.appendChild(a);
      const change = document.createElement('button');
      change.type = 'button';
      change.textContent = 'Change…';
      change.addEventListener('click', () => openPromptPicker(applyPick));
      modeRow.appendChild(change);
      const unlink = document.createElement('button');
      unlink.type = 'button';
      unlink.textContent = 'Unlink';
      unlink.title = "Go back to this chat's own free-text prompt";
      unlink.addEventListener('click', () => { linked = null; renderPromptSource(); });
      modeRow.appendChild(unlink);
      ta.disabled = true;
      if (linked.exists){ showLinkedPreview(linked.uuid); }
      else { ta.value = ''; ta.placeholder = 'No system message will be sent.'; }
    } else {
      const src = document.createElement('span');
      src.className = 'src';
      src.textContent = 'Custom text';
      modeRow.appendChild(src);
      const choose = document.createElement('button');
      choose.type = 'button';
      choose.textContent = 'Choose stored prompt…';
      choose.addEventListener('click', () => openPromptPicker(applyPick));
      modeRow.appendChild(choose);
      ta.disabled = false;
      ta.placeholder = 'Empty = no system message';
      ta.value = customText;
    }
  }
  function applyPick(p){
    linked = {uuid: p.uuid, name: p.name, exists: true};
    renderPromptSource();
  }
  renderPromptSource();
  const save = document.createElement('button');
  save.type = 'button';
  save.className = 'ds-save';
  save.textContent = 'Save';
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      const t = parseInt(timeoutInput.value, 10);
      await putJSON('/chat/api/rooms/' + room + '/settings', {
        system_prompt: customText,
        model_uuid: sel.value || null,
        prompt_uuid: linked ? linked.uuid : null,
        request_timeout: Number.isFinite(t) && t > 0 ? t : null,
      });
      const r = rooms.find(x => x.uuid === room);
      if (r) r.model_uuid = sel.value || null;
      chatToast('Settings saved — applies from the next reply.');
    } catch (e) {
      alert('Save failed: ' + e.message);
    } finally {
      save.disabled = false;
    }
  });
  sidebarEl.appendChild(save);
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
  if (room !== currentRoom || activeSidebarMode() !== 'members') return;  // changed while loading
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
    if (room === currentRoom && activeSidebarMode() === 'members') renderMembers();
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

// Export panel: download or copy the room's history as a JSON document.
// Scope (all / last N) and metadata level (full / minimal) map straight onto
// the /export endpoint's query params; nothing is fetched until a button is
// clicked, so the export always reflects the room's messages at that moment.
// The selections persist in localStorage, so the panel reopens the way it was
// last used (across rooms and reloads).
const EXPORT_PREFS_KEY = 'chat.exportPrefs';
function loadExportPrefs(){
  const prefs = {scope: 'all', lastN: 20, metadata: 'full'};
  try {
    const saved = JSON.parse(localStorage.getItem(EXPORT_PREFS_KEY) || '{}');
    if (saved.scope === 'all' || saved.scope === 'last') prefs.scope = saved.scope;
    const n = parseInt(saved.lastN, 10);
    if (Number.isFinite(n) && n > 0) prefs.lastN = n;
    if (saved.metadata === 'full' || saved.metadata === 'minimal') prefs.metadata = saved.metadata;
  } catch (e) {}
  return prefs;
}
function saveExportPrefs(prefs){
  try { localStorage.setItem(EXPORT_PREFS_KEY, JSON.stringify(prefs)); } catch (e) {}
}
function renderExport(){
  const room = currentRoom;
  const roomObj = currentRoomObj();
  const prefs = loadExportPrefs();
  sidebarEl.innerHTML = '';
  const h = document.createElement('h3');
  h.className = 'sidebar-title';
  h.textContent = 'Export';
  sidebarEl.appendChild(h);

  const scopeLabel = document.createElement('span');
  scopeLabel.className = 'ds-label';
  scopeLabel.textContent = 'Messages';
  sidebarEl.appendChild(scopeLabel);
  const scopeSel = document.createElement('select');
  scopeSel.className = 'ds-model';
  [['all', 'All messages'], ['last', 'Last N messages']].forEach(([v, label]) => {
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = label;
    scopeSel.appendChild(opt);
  });
  scopeSel.value = prefs.scope;
  sidebarEl.appendChild(scopeSel);
  const nInput = document.createElement('input');
  nInput.type = 'number';
  nInput.className = 'ds-timeout';
  nInput.min = '1';
  nInput.step = '1';
  nInput.value = String(prefs.lastN);
  nInput.title = 'How many of the newest messages to export';
  nInput.style.display = prefs.scope === 'last' ? '' : 'none';
  nInput.style.marginTop = '0.4em';
  sidebarEl.appendChild(nInput);
  scopeSel.addEventListener('change', () => {
    nInput.style.display = scopeSel.value === 'last' ? '' : 'none';
    prefs.scope = scopeSel.value;
    saveExportPrefs(prefs);
  });
  nInput.addEventListener('input', () => {
    const n = parseInt(nInput.value, 10);
    if (Number.isFinite(n) && n > 0){ prefs.lastN = n; saveExportPrefs(prefs); }
  });

  const metaLabel = document.createElement('span');
  metaLabel.className = 'ds-label';
  metaLabel.textContent = 'Metadata';
  sidebarEl.appendChild(metaLabel);
  const metaSel = document.createElement('select');
  metaSel.className = 'ds-model';
  [['full', 'Full (uuids, dates, usernames, model)'],
   ['minimal', 'Minimal (user / assistant, text only)']].forEach(([v, label]) => {
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = label;
    metaSel.appendChild(opt);
  });
  metaSel.value = prefs.metadata;
  metaSel.addEventListener('change', () => {
    prefs.metadata = metaSel.value;
    saveExportPrefs(prefs);
  });
  sidebarEl.appendChild(metaSel);

  const fmtLabel = document.createElement('span');
  fmtLabel.className = 'ds-label';
  fmtLabel.textContent = 'Output format';
  sidebarEl.appendChild(fmtLabel);
  const fmt = document.createElement('p');
  fmt.className = 'ds-note';
  fmt.style.margin = '0';
  fmt.textContent = 'JSON';
  sidebarEl.appendChild(fmt);

  // null = invalid "last N" input (already alerted about).
  function exportUrl(){
    let url = '/chat/api/rooms/' + room + '/export?metadata=' + metaSel.value;
    if (scopeSel.value === 'last'){
      const n = parseInt(nInput.value, 10);
      if (!Number.isFinite(n) || n <= 0){
        alert('Enter how many messages to export (a positive number).');
        nInput.focus();
        return null;
      }
      url += '&limit=' + n;
    }
    return url;
  }
  function exportFilename(){
    const slug = ((roomObj && roomObj.name) || 'room')
      .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'room';
    return 'chat-' + slug + '-' + new Date().toISOString().slice(0, 10) + '.json';
  }

  const actions = document.createElement('div');
  actions.style.display = 'flex';
  actions.style.gap = '0.5em';
  const dl = document.createElement('button');
  dl.type = 'button';
  dl.className = 'ds-save';
  dl.textContent = 'Download';
  dl.addEventListener('click', async () => {
    const url = exportUrl();
    if (!url) return;
    dl.disabled = true;
    try {
      const data = await getJSON(url);
      const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = exportFilename();
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      alert('Export failed: ' + e.message);
    } finally {
      dl.disabled = false;
    }
  });
  actions.appendChild(dl);
  const cp = document.createElement('button');
  cp.type = 'button';
  cp.className = 'ds-save';
  cp.textContent = 'Copy to clipboard';
  cp.addEventListener('click', () => {
    const url = exportUrl();
    if (!url) return;
    // copyText accepts a promise-returning source; the button flashes "Copied".
    copyText(() => getJSON(url).then(d => JSON.stringify(d, null, 2)), cp);
  });
  actions.appendChild(cp);
  sidebarEl.appendChild(actions);
}

sidebarModeSel.addEventListener('change', () => {
  const v = sidebarModeSel.value;
  if (v === 'hidden') sidebarVisible = false;
  else { sidebarVisible = true; sidebarMode = v; }
  persistSidebarPrefs();
  renderSidebar();
});

// Ctrl+1 toggles the sidebar (Cmd+B/Cmd+1 belong to the browser). Only
// visibility flips — the panel choice is kept, so showing again returns to
// the last-used panel. A modifier combo, so it also works while typing in
// the composer.
document.addEventListener('keydown', (e) => {
  if (e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey && e.key === '1'){
    e.preventDefault();
    sidebarVisible = !sidebarVisible;
    persistSidebarPrefs();
    syncSidebarModeOptions();
    renderSidebar();
  }
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

// ---- new chatroom modal ----
function selectedRoomType(){
  const checked = document.querySelector('input[name="chat-room-type"]:checked');
  return checked ? checked.value : 'agents';
}
// A direct room's members are fixed (operator + the model responder), so the
// agent picker only applies to agents rooms.
function syncRoomTypeUI(){
  document.getElementById('chat-room-agents').hidden = (selectedRoomType() === 'direct');
}
document.querySelectorAll('input[name="chat-room-type"]').forEach(radio => {
  radio.addEventListener('change', syncRoomTypeUI);
});
async function openRoomModal(){
  const input = document.getElementById('chat-room-input');
  input.value = '';
  if (!agentsLoaded) await loadAgents();
  agentListEl.querySelectorAll('input:checked').forEach(cb => { cb.checked = false; });
  document.querySelector('input[name="chat-room-type"][value="agents"]').checked = true;
  syncRoomTypeUI();
  document.getElementById('chat-room-create').disabled = true;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('chat-room-modal').hidden = false;
  input.focus();
}
function closeRoomModal(){
  document.getElementById('chat-room-modal').hidden = true;
  document.getElementById('ui-modal-backdrop').hidden = true;
}
async function confirmRoomModal(){
  const input = document.getElementById('chat-room-input');
  const name = input.value.trim();
  if (!name){ input.focus(); return; }
  const room_type = selectedRoomType();
  const member_uuids = room_type === 'direct' ? []
    : Array.from(agentListEl.querySelectorAll('input:checked')).map(cb => cb.value);
  try {
    const res = await postJSON('/chat/api/rooms', { name, member_uuids, room_type });
    closeRoomModal();
    await loadRooms(res.uuid);
  } catch (err) {
    alert('Create room failed: ' + err);
  }
}
newRoomBtn.addEventListener('click', openRoomModal);
document.getElementById('chat-room-cancel').addEventListener('click', closeRoomModal);
document.getElementById('chat-room-create').addEventListener('click', confirmRoomModal);
document.getElementById('chat-room-input').addEventListener('input', e => {
  document.getElementById('chat-room-create').disabled = !e.target.value.trim();
});
document.getElementById('chat-room-input').addEventListener('keydown', e => {
  if (e.key === 'Enter'){ e.preventDefault(); confirmRoomModal(); }
});

// ---- stored-prompt picker (Settings sidebar): the /prompt folder tree in a
// modal. Read-only: folders expand/collapse on click (default expanded),
// clicking a prompt hands {uuid, name} to the opener and closes. The pick is
// pending sidebar state until the Save button persists it.
let promptPickerOnPick = null;
const promptPickerExpanded = {};  // folder id -> false when collapsed
async function openPromptPicker(onPick){
  promptPickerOnPick = onPick;
  const treeEl = document.getElementById('chat-prompt-tree');
  treeEl.innerHTML = '<div class="prompt-pick-empty">loading&hellip;</div>';
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('chat-prompt-modal').hidden = false;
  let data = null;
  try { data = await getJSON('/prompt/api/tree'); } catch (_) {}
  if (document.getElementById('chat-prompt-modal').hidden) return;  // closed while loading
  if (!data){
    treeEl.innerHTML = '<div class="prompt-pick-empty">Could not load the prompt tree.</div>';
    return;
  }
  renderPromptPicker(data.folders || [], data.prompts || []);
}
function closePromptPicker(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('chat-prompt-modal').hidden = true;
  promptPickerOnPick = null;
}
function renderPromptPicker(folders, prompts){
  const treeEl = document.getElementById('chat-prompt-tree');
  if (!folders.length && !prompts.length){
    treeEl.innerHTML = '<div class="prompt-pick-empty">No stored prompts yet — ' +
      'create some on the <a href="/prompt" target="_blank">Prompt</a> page.</div>';
    return;
  }
  const childFolders = pid => folders.filter(f => (f.parentId || null) === pid);
  const promptsIn = id => prompts.filter(p => (p.folderId || null) === id);
  const isOpen = id => promptPickerExpanded[id] !== false;
  const leafDiv = p => {
    const el = document.createElement('div');
    el.className = 'prompt-pick-leaf';
    el.textContent = p.name;
    el.addEventListener('click', () => {
      const cb = promptPickerOnPick;
      closePromptPicker();
      if (cb) cb(p);
    });
    return el;
  };
  const folderLi = f => {
    const li = document.createElement('li');
    const kids = childFolders(f.id);
    const leaves = promptsIn(f.id);
    const hasKids = (kids.length + leaves.length) > 0;
    const node = document.createElement('div');
    node.className = 'prompt-pick-node';
    const ic = document.createElement('span');
    ic.className = 'chat-ficon';
    ic.innerHTML = (isOpen(f.id) && hasKids) ? CHAT_ICON_FOLDER_OPEN : CHAT_ICON_FOLDER;
    const label = document.createElement('span');
    label.textContent = f.name;
    node.appendChild(ic);
    node.appendChild(label);
    node.addEventListener('click', () => {
      promptPickerExpanded[f.id] = !isOpen(f.id);
      renderPromptPicker(folders, prompts);
    });
    li.appendChild(node);
    if (isOpen(f.id) && hasKids){
      const ul = document.createElement('ul');
      kids.forEach(c => ul.appendChild(folderLi(c)));
      leaves.forEach(p => { const pli = document.createElement('li'); pli.appendChild(leafDiv(p)); ul.appendChild(pli); });
      li.appendChild(ul);
    }
    return li;
  };
  const ul = document.createElement('ul');
  childFolders(null).forEach(f => ul.appendChild(folderLi(f)));
  promptsIn(null).forEach(p => { const li = document.createElement('li'); li.appendChild(leafDiv(p)); ul.appendChild(li); });
  treeEl.replaceChildren(ul);
}
document.getElementById('chat-prompt-cancel').addEventListener('click', closePromptPicker);

// Close whichever chat modal is open; each close fn clears its own state.
function closeOpenModal(){
  if (!document.getElementById('chat-folder-modal').hidden) closeFolderModal();
  if (!document.getElementById('chat-delete-modal').hidden) closeDeleteModal();
  if (!document.getElementById('chat-room-modal').hidden) closeRoomModal();
  if (!document.getElementById('chat-prompt-modal').hidden) closePromptPicker();
  if (!document.getElementById('chat-rename-modal').hidden) closeChatRenameModal();
}
// Has the user typed/checked anything in the currently open modal? If so we
// refuse the accidental dismiss paths (outside-click / Esc) so no input is lost.
function openModalDirty(){
  if (!document.getElementById('chat-folder-modal').hidden){
    return document.getElementById('chat-folder-input').value !== ((folderModalState && folderModalState.current) || '');
  }
  if (!document.getElementById('chat-delete-modal').hidden){
    return document.getElementById('chat-delete-input').value !== '';
  }
  if (!document.getElementById('chat-room-modal').hidden){
    return document.getElementById('chat-room-input').value !== ''
      || agentListEl.querySelectorAll('input:checked').length > 0;
  }
  // Rename: dirty once the typed name differs from the stored one — only the
  // explicit Rename/Cancel buttons close it then.
  if (!document.getElementById('chat-rename-modal').hidden){
    return document.getElementById('chat-rename-input').value !== (chatRenameOriginal || '');
  }
  return false;
}
// Dismiss by clicking the shared backdrop (outside any open card) or pressing
// Esc — but only when the modal is untouched, so an accidental click/keystroke
// can't discard typed-in data. The Cancel button stays an explicit way out.
function dismissOpenModalIfClean(){
  if (!openModalDirty()) closeOpenModal();
}
document.getElementById('ui-modal-backdrop').addEventListener('click', dismissOpenModalIfClean);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') dismissOpenModalIfClean();
});

// Live updates: the server pushes {room_uuid, message_id, event, kind, ...}
// on every message insert/update/edit/delete. EventSource ignores `:` comment
// lines (the keepalives), so only real events reach onmessage.

// Reflect one room's unread count in the left panel WITHOUT renderRooms().
// A full rebuild replaces every room anchor; done per SSE event while another
// room is active, it can destroy the node between mousedown and mouseup so
// clicks on rooms never land. Patch the single badge in place instead — any
// later full render reads unread[] and agrees.
function bumpUnreadBadge(roomUuid){
  const btn = roomsEl.querySelector('.room[data-room="' + roomUuid + '"]');
  if (!btn) return;  // row not rendered (room inside a collapsed folder)
  const n = unread[roomUuid] || 0;
  let dot = btn.querySelector('.unread');
  if (n <= 0){
    if (dot) dot.remove();
    return;
  }
  if (!dot){
    dot = document.createElement('span');
    dot.className = 'unread';
    btn.appendChild(dot);
  }
  dot.textContent = n;
}

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
      // Only a NEW real message counts as unread: event 'insert' of
      // kind 'message'. Streaming token batches (event 'update') reuse one
      // message_id and must not re-count it; edits, deletions, and
      // debug/thinking/progress rows aren't new messages at all.
      if (d.event === 'insert' && d.kind === 'message'){
        unread[d.room_uuid] = (unread[d.room_uuid] || 0) + 1;
        bumpUnreadBadge(d.room_uuid);
      }
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

// Root drop targets: the "Move to top level" zone + empty space in the rooms
// panel both move a dragged node to the root level.
wireRootDrop(document.getElementById('chat-root-drop'), false);
(function wireRoomsContainerRootDrop(){
  const panel = document.querySelector('.rooms');
  panel.addEventListener('dragover', e => {
    if (dragNode){ e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
  });
  panel.addEventListener('drop', e => {
    if (dragNode){ e.preventDefault(); dropInto(null, false); }
  });
})();

// Deep link: ?id=<uuid> reopens that room or folder on load (mirrors /cron).
// ?msg=<message-id> additionally scrolls to + highlights that message (e.g. the
// /assistant inspector's "open in chat" link to a run's triggering message).
(function(){
  const p = new URLSearchParams(window.location.search);
  loadRooms(p.get('id'), p.get('msg'));
})();
startStream();
</script>
"""
