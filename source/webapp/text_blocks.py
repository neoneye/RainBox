"""A labelled block of text, as a pane on /assistant draws one, for the other
pages that show a prompt or a reply.

`text_block` is the markup: the same `_block_html` the assistant page's panes
are built from, so a prompt reads the same wherever it is shown. A collapsed
block's summary carries its character count. Every block has a `copy`
button, and one whose text is a JSON document has the `raw | pretty` switch.

`BLOCKS_STYLE` and `BLOCKS_SCRIPT` are what a page includes once to make the
markup work. The styles apply inside an element of class `tb-scope`. The
script keeps the JSON reading under the key /assistant uses, so the choice
made on one page holds on the other.

/assistant itself does not include these. Its own script does the same work
and more: the cache view repaints the same panes, and the two are one piece
of code there.

The script is a non-raw Python string that ends up inline in a page, so it
holds no backslash.
"""
from __future__ import annotations

from typing import Any

from markupsafe import Markup


def text_block(title: str, body: Any, *, collapsed: bool = False,
               key: str = "") -> Markup:
    """One block, or nothing when there is no text. `key` names a collapsed
    block across a page's live reload, so one that was open stays open."""
    # Imported here so a page can take the style and the script from this
    # module without loading the assistant page's components to do it.
    from webapp.assistant_components import _block, _block_html

    return _block_html(_block(title, body, collapsed=collapsed, key=key))


BLOCKS_STYLE = """
<style>
  .tb-scope .ev-block{margin-bottom:0.7rem}
  .tb-scope .ev-block h5{margin:0 0 0.2rem;font-size:0.68rem;font-weight:700;
    letter-spacing:0.05em;text-transform:uppercase;color:#6b7280}
  .tb-scope details.ev-block > summary{font-size:0.68rem;font-weight:700;
    text-transform:uppercase;letter-spacing:0.05em;color:#6b7280;cursor:pointer;
    margin-bottom:0.15rem;-webkit-user-select:none;user-select:none}
  .tb-scope details.ev-block > summary:hover{color:#1a1a2e}
  .tb-scope .ev-pre{margin:0;padding:10px 12px;background:#f8fafc;
    border:1px solid #eef0f4;border-radius:6px;
    font-family:ui-monospace,Menlo,monospace;font-size:0.78rem;
    white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;color:#1a1a2e}
  /* A long block is cut to a few lines with a toggle under it, so the page
     is the only thing that scrolls. */
  .tb-scope .ev-pre.clamped{overflow-y:hidden;
    -webkit-mask-image:linear-gradient(#000 72%, transparent);
    mask-image:linear-gradient(#000 72%, transparent)}
  .tb-scope .ev-more{display:block;margin:0.25rem 0 0;padding:0;border:0;
    background:none;font:inherit;font-size:0.72rem;color:#2563eb;cursor:pointer;
    -webkit-user-select:none;user-select:none}
  .tb-scope .ev-more:hover{text-decoration:underline}
  .tb-scope .ev-acts{display:inline-flex;gap:0.35rem;align-items:center;
    margin-left:0.5rem;vertical-align:middle}
  .tb-scope .ev-view{display:inline-flex;vertical-align:middle;
    border:1px solid #e5e7eb;border-radius:5px;overflow:hidden}
  .tb-scope .ev-view button{border:0;background:#fff;cursor:pointer;font:inherit;
    font-size:0.62rem;font-weight:400;letter-spacing:0.03em;text-transform:lowercase;
    color:#98a2b3;padding:0 0.4rem;line-height:1.5}
  .tb-scope .ev-view button + button{border-left:1px solid #e5e7eb}
  .tb-scope .ev-view button:hover{color:#1a1a2e;background:#f3f4f6}
  .tb-scope .ev-view button.on{background:#eef2ff;color:#2563eb;font-weight:600}
  .tb-scope .ev-copy{border:1px solid #e5e7eb;border-radius:5px;background:#fff;
    cursor:pointer;font:inherit;font-size:0.62rem;font-weight:400;
    letter-spacing:0.03em;text-transform:lowercase;color:#98a2b3;
    padding:0 0.4rem;line-height:1.5}
  .tb-scope .ev-copy:hover{color:#1a1a2e;background:#f3f4f6}
  .tb-toast{position:fixed;bottom:18px;right:18px;max-width:420px;
    background:#1f2937;color:#fff;padding:10px 14px;border-radius:8px;
    font-size:0.84rem;opacity:0;pointer-events:none;transition:opacity 0.2s;
    z-index:50}
  .tb-toast.show{opacity:1}
</style>
"""


BLOCKS_SCRIPT = """
<div id="tb-toast" class="tb-toast"></div>
<script>
(function () {
  // How many lines of a long block are shown before its toggle.
  var CLAMP_LINES = 6;
  // One reading of JSON for every page that shows a block, kept where
  // /assistant keeps it. Raw is the default: a block holds the bytes that
  // were recorded.
  var JSON_VIEW_KEY = 'assistant.json-view';
  var mode = 'raw';
  try {
    if (localStorage.getItem(JSON_VIEW_KEY) === 'pretty') { mode = 'pretty'; }
  } catch (e) {}  // storage unavailable: this visit starts on raw

  function applyJsonView(root) {
    root.querySelectorAll('pre.ev-pre[data-json]').forEach(function (pre) {
      // The recorded bytes, kept on the node the first time it is touched:
      // switching back returns the original, not a re-serialization.
      if (pre.dataset.raw === undefined) { pre.dataset.raw = pre.textContent; }
      if (mode !== 'pretty') { pre.textContent = pre.dataset.raw; return; }
      try {
        // No key sorting: the order a reply was written in is part of it.
        pre.textContent = JSON.stringify(JSON.parse(pre.dataset.raw), null, 2);
      } catch (e) {
        pre.textContent = pre.dataset.raw;
      }
    });
    root.querySelectorAll('.ev-view button').forEach(function (b) {
      var on = b.getAttribute('data-view') === mode;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  // A block is measurable only while it is shown, so a collapsed one is
  // measured when it opens. Once per block.
  function clampBlocks(root) {
    root.querySelectorAll('.ev-pre').forEach(function (pre) {
      if (pre.dataset.clamped || !pre.offsetHeight) { return; }
      pre.dataset.clamped = '1';
      var cs = getComputedStyle(pre);
      var line = parseFloat(cs.lineHeight);
      if (!line) { line = parseFloat(cs.fontSize) * 1.2; }
      var max = Math.round(line * CLAMP_LINES);
      // A block that already fits gets no toggle.
      if (pre.scrollHeight <= max + 4) { return; }
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ev-more';
      function apply(on) {
        pre.classList.toggle('clamped', on);
        pre.style.maxHeight = on ? max + 'px' : '';
        btn.textContent = on ? 'show more' : 'show less';
      }
      btn.addEventListener('click', function () {
        apply(!pre.classList.contains('clamped'));
      });
      apply(true);
      pre.parentNode.insertBefore(btn, pre.nextSibling);
    });
  }

  function toast(msg) {
    var t = document.getElementById('tb-toast');
    if (!t) { return; }
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(function () { t.classList.remove('show'); }, 3500);
  }

  // Copy, and establish that it happened. execCommand returning true is not
  // that: an extension can answer true and copy nothing. The `copy` event
  // fires only on a real copy, so that is what is watched for.
  function verifiedCopy(text) {
    if (!document.execCommand) { return 'unavailable'; }
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    var previous = document.activeElement;
    ta.select();
    var copied = false;
    var witness = function () { copied = true; };
    document.addEventListener('copy', witness, true);
    var claimed = false;
    try { claimed = document.execCommand('copy'); } catch (e) { claimed = false; }
    document.removeEventListener('copy', witness, true);
    document.body.removeChild(ta);
    if (previous && previous.focus) { previous.focus(); }
    if (claimed && copied) { return 'ok'; }
    return claimed ? 'blocked' : 'unavailable';
  }
  function copyText(text, what) {
    var done = function (ok) {
      toast(ok === false ? 'Could not copy. Select the text instead.'
                         : 'Copied ' + (what || 'to the clipboard'));
    };
    var status = verifiedCopy(text);
    if (status !== 'unavailable') { done(status === 'ok'); return; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { done(true); }, function () { done(false); });
    } else {
      done(false);
    }
  }

  document.addEventListener('click', function (ev) {
    var b = ev.target.closest && ev.target.closest('.ev-view button');
    if (!b) { return; }
    // Inside a <summary> the click would also fold the block it labels.
    ev.preventDefault();
    ev.stopPropagation();
    mode = b.getAttribute('data-view') === 'pretty' ? 'pretty' : 'raw';
    try { localStorage.setItem(JSON_VIEW_KEY, mode); }
    catch (e) {}  // it still switches; it will not be remembered
    applyJsonView(document);
  });
  // What is copied is what the block is showing: a reader who switched it
  // to indented JSON asked for that reading.
  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest && ev.target.closest('.ev-copy');
    if (!btn) { return; }
    ev.preventDefault();
    ev.stopPropagation();
    var block = btn.closest('.ev-block');
    var pre = block && block.querySelector('pre.ev-pre');
    if (!pre) { return; }
    var name = block.querySelector('h5, summary');
    copyText(pre.textContent,
             name ? '“' + name.firstChild.textContent.trim() + '”' : null);
  });
  document.addEventListener('toggle', function (ev) {
    if (ev.target.classList && ev.target.classList.contains('ev-block')) {
      clampBlocks(ev.target);
    }
  }, true);

  // For a page that swaps its content in: call this on what was swapped.
  window.tbApply = function (root) {
    applyJsonView(root);
    clampBlocks(root);
  };
  window.tbApply(document);
})();
</script>
"""
