"""The `analyze` overlay on a prompt block: a treemap of what the prompt is
made of, by characters.

A page that draws prompt blocks includes `PROMPT_TREEMAP` once, outside any
region a live refresh swaps. A block opts in by carrying an `.ev-analyze`
button inside its `.ev-block`; the click reads the block's `pre.ev-pre` text,
so the overlay measures exactly the bytes the pane shows.

Everything happens in the browser, from the text alone:

- **Sections.** An `<name ...>` with a matching `</name>` is a section, nested
  as the prompt nests them. A tag with no close (prose that mentions
  `<the fact's uuid>`) stays text.
- **Inside a long text run.** Markdown headings split it into one part per
  heading; with no headings, blank-line paragraphs do. Short runs stay one
  cell, so a chat message is not shredded into its paragraphs.
- **Shortened parts.** A section whose tag carries `truncated="..."`, text
  holding the middle-truncation marker, a recalled fact tagged `truncateN`,
  and an `<omitted count="N"/>` element. These are the shapes
  `agents.base.truncate_middle` and the assistant's prompt builders write.

The string is raw: the page templates are ordinary Python strings, where a
backslash in inline JavaScript is consumed before the browser sees it. It
reaches the template as a value (`{{ prompt_treemap|safe }}`), so Jinja never
parses the braces in it either.
"""

PROMPT_TREEMAP: str = r"""
<style>
  .pt-overlay { position:fixed; inset:0; z-index:1500; background:rgba(17,24,39,0.55);
                display:flex; align-items:stretch; justify-content:center; padding:2.5vh 2.5vw; }
  .pt-overlay[hidden] { display:none; }
  .pt-box { flex:1; min-width:0; display:flex; flex-direction:column; background:#fff;
            border-radius:8px; box-shadow:0 10px 40px rgba(0,0,0,0.35); overflow:hidden;
            font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:#1a1a2e; }
  .pt-head { display:flex; align-items:baseline; gap:0.9rem; padding:0.55rem 0.8rem;
             border-bottom:1px solid #e5e7eb; font-size:0.8rem; }
  .pt-title { font-weight:600; }
  .pt-sub { color:#6b7280; }
  .pt-legend { color:#6b7280; margin-left:auto; display:inline-flex; align-items:center; gap:0.35rem; }
  .pt-swatch { display:inline-block; width:1.5em; height:0.9em; border:1px solid #9ca3af;
               border-radius:2px; background-color:#e5e7eb; }
  .pt-close { border:1px solid #e5e7eb; border-radius:5px; background:#fff; cursor:pointer;
              font:inherit; color:#6b7280; padding:0.05rem 0.5rem; }
  .pt-close:hover { color:#1a1a2e; background:#f3f4f6; }
  .pt-crumbs { padding:0.3rem 0.8rem; border-bottom:1px solid #e5e7eb; font-size:0.74rem;
               color:#6b7280; min-height:1.2em; }
  .pt-crumbs button { border:0; background:none; padding:0; font:inherit; color:#2563eb;
                      cursor:pointer; }
  .pt-map { position:relative; flex:1; min-height:0; margin:6px; }
  .pt-cell { position:absolute; box-sizing:border-box; overflow:hidden; border-radius:3px;
             font-size:11px; line-height:1.25; color:#1a1a2e; cursor:default; }
  .pt-cell.pt-group { cursor:zoom-in; }
  .pt-cell.pt-hover { outline:2px solid #1a1a2e; outline-offset:-2px; z-index:2; }
  .pt-name { display:block; padding:2px 4px 0; white-space:nowrap; overflow:hidden;
             text-overflow:ellipsis; font-weight:600; }
  .pt-size { display:block; padding:0 4px; white-space:nowrap; overflow:hidden;
             color:#374151; font-variant-numeric:tabular-nums; }
  .pt-group > .pt-name { font-weight:700; }
  .pt-group > .pt-name .pt-size { display:inline; padding:0 0 0 0.4em; font-weight:400; }
  /* Shortened before it was sent: a hatch over the cell's own colour, so the
     section is still recognisable and the mark does not depend on hue. */
  .pt-cut, .pt-swatch { background-image:repeating-linear-gradient(
        45deg, rgba(17,24,39,0.22) 0, rgba(17,24,39,0.22) 2px, transparent 2px, transparent 7px); }
  .pt-tip { position:fixed; z-index:1600; max-width:420px; pointer-events:none; background:#1f2937;
            color:#f9fafb; border-radius:6px; padding:0.45rem 0.6rem; font-size:0.74rem;
            line-height:1.4; box-shadow:0 4px 14px rgba(0,0,0,0.35);
            font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
  .pt-tip[hidden] { display:none; }
  .pt-tip b { font-weight:600; }
  .pt-tip .pt-path { color:#9ca3af; }
  .pt-tip .pt-warn { color:#fcd34d; }
  .pt-tip .pt-peek { display:block; margin-top:0.3rem; padding-top:0.3rem;
                     border-top:1px solid #4b5563; color:#d1d5db; white-space:pre-wrap;
                     word-break:break-word; font-family:ui-monospace,Menlo,monospace;
                     font-size:0.68rem; }
</style>
<div id="pt-overlay" class="pt-overlay" hidden>
  <div class="pt-box" role="dialog" aria-modal="true" aria-label="Prompt breakdown">
    <div class="pt-head">
      <span class="pt-title" id="pt-title"></span>
      <span class="pt-sub" id="pt-sub"></span>
      <span class="pt-legend"><i class="pt-swatch"></i>shortened before it was sent</span>
      <button type="button" class="pt-close" id="pt-close" title="Close (Esc)">close</button>
    </div>
    <div class="pt-crumbs" id="pt-crumbs"></div>
    <div class="pt-map" id="pt-map"></div>
  </div>
</div>
<div id="pt-tip" class="pt-tip" hidden></div>
<script>
(function () {
  // Long enough to be worth opening up. Below these a run of text is one cell.
  var HEADING_SPLIT_MIN = 1500;
  var PARAGRAPH_SPLIT_MIN = 3000;
  // One hue per top-level section, in the order the prompt lists them, so a
  // section keeps its colour from one call to the next. Past eight: grey.
  var HUES = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300',
              '#6250d6', '#e34948'];
  var GREY = '#8b93a1';
  var TRUNCATION_ATTRS = {truncated: 1, original_chars: 1, included_chars: 1};

  // ---- reading the prompt ---------------------------------------------------

  function parseAttrs(src) {
    var out = {}, re = /([A-Za-z_][\w.-]*)\s*=\s*"([^"]*)"/g, m;
    while ((m = re.exec(src || ''))) { out[m[1]] = m[2]; }
    return out;
  }

  // The end of the close tag that matches the `name` opened just before
  // `from`, counting same-named sections nested inside. -1 when there is none.
  function closeOf(text, name, from, to) {
    var re = new RegExp('<(/?)' + name.replace(/[.\-]/g, '\\$&') + '(?=[\\s>/])[^<>]*>', 'g');
    re.lastIndex = from;
    var depth = 1, m;
    while ((m = re.exec(text)) && m.index < to) {
      if (m[1]) { depth -= 1; if (!depth) { return {at: m.index, end: re.lastIndex}; } }
      else if (m[0].slice(-2) !== '/>') { depth += 1; }
    }
    return null;
  }

  function firstWords(s, n) {
    var flat = s.replace(/\s+/g, ' ').trim();
    return flat.length > n ? flat.slice(0, n).trim() + '…' : flat;
  }

  function cutNotes(node, text, attrs) {
    var notes = [];
    if (attrs && attrs.truncated) {
      var kept = attrs.included_chars, was = attrs.original_chars;
      notes.push('truncated (' + attrs.truncated + ')'
                 + (was ? ': ' + Number(was).toLocaleString() + ' chars originally'
                    + (kept ? ', ' + Number(kept).toLocaleString() + ' kept' : '') : ''));
    }
    if (!node.kids.length) {
      var body = text.slice(node.start, node.end), m;
      var dropped = 0, re = /\[… (\d+) characters dropped from the middle …\]/g;
      while ((m = re.exec(body))) { dropped += Number(m[1]); }
      // The tag already said so, with the counts; the marker adds nothing.
      if (dropped && !notes.length) {
        notes.push(dropped.toLocaleString() + ' chars dropped from the middle');
      }
      var facts = (body.match(/\btruncate\d+:/g) || []).length;
      if (facts) { notes.push(facts + ' recalled fact' + (facts > 1 ? 's' : '') + ' shortened'); }
      re = /<omitted\s+count="(\d+)"/g;
      var gone = 0;
      while ((m = re.exec(body))) { gone += Number(m[1]); }
      if (gone) { notes.push(gone + ' item' + (gone > 1 ? 's' : '') + ' omitted'); }
      re = /(\d+) lower-ranked fact\(s\) omitted/g;
      while ((m = re.exec(body))) { notes.push(m[1] + ' lower-ranked facts omitted'); }
    }
    node.cut = notes;
  }

  function leaf(text, name, start, end, kind) {
    var node = {name: name, start: start, end: end, kids: [], kind: kind};
    cutNotes(node, text, null);
    return node;
  }

  // A run of text between sections, as zero or more parts.
  function textParts(text, start, end) {
    var body = text.slice(start, end);
    if (!body.trim()) { return []; }
    var size = body.length, parts = [];
    if (size >= HEADING_SPLIT_MIN) {
      // Headings, skipping fenced code where a `#` line is a comment.
      var marks = [], offset = 0, fenced = false;
      body.split('\n').forEach(function (line) {
        if (/^\s*(```|~~~)/.test(line)) { fenced = !fenced; }
        else if (!fenced && /^#{1,6} +\S/.test(line)) {
          marks.push({at: offset, name: line.replace(/^#+ +/, '').trim()});
        }
        offset += line.length + 1;
      });
      if (marks.length) {
        if (body.slice(0, marks[0].at).trim()) {
          parts.push(leaf(text, firstWords(body.slice(0, marks[0].at), 40),
                          start, start + marks[0].at, 'text'));
        }
        marks.forEach(function (mark, i) {
          var stop = i + 1 < marks.length ? marks[i + 1].at : size;
          parts.push(leaf(text, mark.name, start + mark.at, start + stop, 'heading'));
        });
        return parts;
      }
    }
    if (size >= PARAGRAPH_SPLIT_MIN) {
      var re = /\n[ \t]*\n/g, from = 0, m, spans = [];
      while ((m = re.exec(body))) { spans.push([from, m.index]); from = re.lastIndex; }
      spans.push([from, size]);
      spans = spans.filter(function (s) { return body.slice(s[0], s[1]).trim(); });
      if (spans.length > 1) {
        return spans.map(function (s) {
          return leaf(text, firstWords(body.slice(s[0], s[1]), 40),
                      start + s[0], start + s[1], 'paragraph');
        });
      }
    }
    return [leaf(text, firstWords(body, 40), start, end, 'text')];
  }

  function children(text, start, end) {
    var open = /<([A-Za-z_][\w.-]*)((?:\s[^<>]*)?)>/g, kids = [], at = start, m;
    open.lastIndex = start;
    while ((m = open.exec(text)) && m.index < end) {
      if (m[2].slice(-1) === '/') { continue; }
      var close = closeOf(text, m[1], open.lastIndex, end);
      if (!close) { continue; }
      kids = kids.concat(textParts(text, at, m.index));
      var attrs = parseAttrs(m[2]);
      var extra = Object.keys(attrs).filter(function (k) { return !TRUNCATION_ATTRS[k]; })
        .map(function (k) { return attrs[k]; }).join(' ');
      // A role says more than the tag it sits on: thirty cells reading
      // `message` tell the reader nothing once the label is clipped.
      var node = {name: (attrs.role ? '' : m[1] + (extra ? ' · ' : ''))
                        + firstWords(extra, 40),
                  start: m.index, end: close.end, kind: 'section', kids: []};
      var inner = children(text, open.lastIndex, close.at);
      // A section holding one plain run of text is itself the cell.
      if (!(inner.length === 1 && inner[0].kind === 'text')) { node.kids = inner; }
      cutNotes(node, text, attrs);
      kids.push(node);
      at = close.end;
      open.lastIndex = close.end;
    }
    return kids.concat(textParts(text, at, end));
  }

  function measure(node, parent) {
    node.parent = parent || null;
    node.size = node.end - node.start;
    node.cutInside = 0;
    node.kids.forEach(function (kid) {
      measure(kid, node);
      node.cutInside += kid.cutInside + (kid.cut.length ? 1 : 0);
    });
    // What a parent's area is divided by: its parts, not its own tags.
    node.area = node.kids.length
      ? node.kids.reduce(function (sum, kid) { return sum + kid.size; }, 0) : node.size;
  }

  function parsePrompt(text, title) {
    var root = {name: title, start: 0, end: text.length, kind: 'root', cut: [],
                kids: children(text, 0, text.length)};
    measure(root, null);
    root.kids.forEach(function (kid, i) {
      (function paint(node) { node.hue = i < HUES.length ? HUES[i] : GREY;
                              node.kids.forEach(paint); })(kid);
    });
    return root;
  }

  // ---- laying it out --------------------------------------------------------

  // Squarified treemap: fill the rectangle strip by strip along its shorter
  // side, closing a strip when one more cell would make its cells less square.
  function squarify(nodes, x, y, w, h) {
    var items = nodes.filter(function (n) { return n.size > 0; })
      .sort(function (a, b) { return b.size - a.size; });
    var total = items.reduce(function (sum, n) { return sum + n.size; }, 0);
    if (!total || w <= 0 || h <= 0) { return []; }
    var scale = w * h / total, i = 0;
    function worst(lo, hi, sum, side) {
      var big = items[lo].size * scale, small = items[hi - 1].size * scale;
      return Math.max(side * side * big / (sum * sum), sum * sum / (side * side * small));
    }
    while (i < items.length) {
      var side = Math.min(w, h), j = i + 1, sum = items[i].size * scale;
      while (j < items.length) {
        var next = sum + items[j].size * scale;
        if (worst(i, j + 1, next, side) > worst(i, j, sum, side)) { break; }
        sum = next; j += 1;
      }
      var thick = sum / side, run = 0;
      for (var k = i; k < j; k += 1) {
        var len = items[k].size * scale / thick;
        if (w >= h) { items[k].box = {x: x, y: y + run, w: thick, h: len}; }
        else { items[k].box = {x: x + run, y: y, w: len, h: thick}; }
        run += len;
      }
      if (w >= h) { x += thick; w -= thick; } else { y += thick; h -= thick; }
      i = j;
    }
    return items;
  }

  // ---- drawing --------------------------------------------------------------

  var overlay = document.getElementById('pt-overlay');
  var map = document.getElementById('pt-map');
  var tip = document.getElementById('pt-tip');
  var crumbs = document.getElementById('pt-crumbs');
  var state = {root: null, focus: null, text: ''};
  var HEADER = 17, GAP = 2;

  function chars(n) { return n.toLocaleString() + ' chars'; }
  function share(part, whole) {
    var pct = whole ? 100 * part / whole : 0;
    return (pct >= 10 ? pct.toFixed(0) : pct.toFixed(1)) + '%';
  }

  function cell(node, box, depth) {
    var el = document.createElement('div');
    var grouped = node.kids.length && box.w > 70 && box.h > 2 * HEADER + 8;
    el.className = 'pt-cell' + (grouped ? ' pt-group' : '')
      + (node.cut.length ? ' pt-cut' : '');
    el.style.left = box.x + 'px'; el.style.top = box.y + 'px';
    el.style.width = Math.max(0, box.w - GAP) + 'px';
    el.style.height = Math.max(0, box.h - GAP) + 'px';
    // A frame is deeper than what it holds, so nesting reads without borders.
    var mix = grouped ? Math.min(62, 46 + 8 * depth) : 26;
    el.style.backgroundColor = 'color-mix(in srgb, ' + node.hue + ' ' + mix + '%, #fff)';
    el._node = node;
    var name = document.createElement('span');
    name.className = 'pt-name';
    name.textContent = (node.cut.length ? '✂ ' : '') + node.name;
    var size = document.createElement('span');
    size.className = 'pt-size';
    size.textContent = chars(node.size) + ' · ' + share(node.size, state.root.size);
    if (grouped) {
      name.appendChild(size);
      el.appendChild(name);
      squarify(node.kids, GAP, HEADER, box.w - 2 * GAP, box.h - GAP - HEADER)
        .forEach(function (kid) { el.appendChild(cell(kid, kid.box, depth + 1)); });
    } else {
      if (box.w > 34 && box.h > 16) { el.appendChild(name); }
      if (box.w > 60 && box.h > 32) { el.appendChild(size); }
    }
    return el;
  }

  function pathOf(node) {
    var out = [];
    for (var n = node; n; n = n.parent) { out.unshift(n); }
    return out;
  }

  function draw() {
    map.replaceChildren();
    tip.hidden = true;
    var focus = state.focus, w = map.clientWidth, h = map.clientHeight;
    var nodes = focus.kids.length ? focus.kids : [focus];
    squarify(nodes, 0, 0, w + GAP, h + GAP).forEach(function (node) {
      map.appendChild(cell(node, node.box, 0));
    });
    crumbs.replaceChildren();
    pathOf(focus).forEach(function (node, i, all) {
      if (i) { crumbs.appendChild(document.createTextNode('  ›  ')); }
      if (i === all.length - 1) {
        crumbs.appendChild(document.createTextNode(node.name));
      } else {
        var b = document.createElement('button');
        b.type = 'button'; b.textContent = node.name; b._node = node;
        crumbs.appendChild(b);
      }
    });
    if (focus === state.root) {
      crumbs.appendChild(document.createTextNode(
        '   — click a section to open it'));
    }
  }

  function line(parent, cls, text) {
    var el = document.createElement('div');
    if (cls) { el.className = cls; }
    el.textContent = text;
    parent.appendChild(el);
    return el;
  }

  function showTip(node, ev) {
    tip.replaceChildren();
    var trail = pathOf(node).slice(1, -1).map(function (n) { return n.name; });
    if (trail.length) { line(tip, 'pt-path', trail.join(' › ')); }
    var head = document.createElement('b');
    head.textContent = node.name;
    tip.appendChild(head);
    line(tip, '', chars(node.size) + ' · ' + share(node.size, state.root.size)
         + ' of the prompt'
         + (node.parent && node.parent !== state.root
            ? ' · ' + share(node.size, node.parent.size) + ' of ' + node.parent.name : ''));
    if (node.kids.length) {
      line(tip, '', node.kids.length + ' part' + (node.kids.length > 1 ? 's' : '')
           + (node.cutInside ? ', ' + node.cutInside + ' shortened' : ''));
    }
    node.cut.forEach(function (note) { line(tip, 'pt-warn', '✂ ' + note); });
    var peek = state.text.slice(node.start, Math.min(node.end, node.start + 220));
    line(tip, 'pt-peek', peek.trim() + (node.size > 220 ? '…' : ''));
    tip.hidden = false;
    var pad = 14, tw = tip.offsetWidth, th = tip.offsetHeight;
    var x = ev.clientX + pad, y = ev.clientY + pad;
    if (x + tw > window.innerWidth - 4) { x = ev.clientX - tw - pad; }
    if (y + th > window.innerHeight - 4) { y = ev.clientY - th - pad; }
    tip.style.left = Math.max(4, x) + 'px';
    tip.style.top = Math.max(4, y) + 'px';
  }

  var hovered = null;
  function hover(el) {
    if (hovered === el) { return; }
    if (hovered) { hovered.classList.remove('pt-hover'); }
    hovered = el;
    if (el) { el.classList.add('pt-hover'); }
  }
  map.addEventListener('mousemove', function (ev) {
    var el = ev.target.closest('.pt-cell');
    hover(el);
    if (el) { showTip(el._node, ev); } else { tip.hidden = true; }
  });
  map.addEventListener('mouseleave', function () { hover(null); tip.hidden = true; });
  // A click opens the outermost section under the pointer that is not open
  // yet, one level at a time; the trail above the map walks back out.
  map.addEventListener('click', function (ev) {
    var el = ev.target.closest('.pt-cell');
    if (!el) { return; }
    var trail = pathOf(el._node), at = trail.indexOf(state.focus);
    var next = null;
    for (var i = at + 1; i < trail.length; i += 1) {
      if (trail[i].kids.length) { next = trail[i]; break; }
    }
    if (next) { state.focus = next; hovered = null; draw(); }
  });
  crumbs.addEventListener('click', function (ev) {
    if (ev.target._node) { state.focus = ev.target._node; hovered = null; draw(); }
  });

  function close() { overlay.hidden = true; tip.hidden = true; map.replaceChildren(); }
  function openFor(text, title) {
    state.text = text;
    state.root = parsePrompt(text, title);
    state.focus = state.root;
    document.getElementById('pt-title').textContent = title;
    var cut = state.root.cutInside;
    document.getElementById('pt-sub').textContent = chars(text.length)
      + ' · ' + state.root.kids.length + ' top-level parts'
      + (cut ? ' · ' + cut + ' shortened' : ' · nothing shortened');
    overlay.hidden = false;
    draw();
  }
  document.getElementById('pt-close').addEventListener('click', close);
  overlay.addEventListener('click', function (ev) { if (ev.target === overlay) { close(); } });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && !overlay.hidden) { close(); }
  });
  window.addEventListener('resize', function () { if (!overlay.hidden) { draw(); } });

  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest && ev.target.closest('.ev-analyze');
    if (!btn) { return; }
    // Inside a <summary> the click would also fold the block it is labelling.
    ev.preventDefault();
    ev.stopPropagation();
    var block = btn.closest('.ev-block');
    var pre = block && block.querySelector('pre.ev-pre');
    if (!pre) { return; }
    var name = block.querySelector('h5, summary');
    openFor(pre.textContent, name ? name.firstChild.textContent.replace(/\s*\(\d+ chars\)\s*$/, '').trim() : 'prompt');
  });

  // For a console or a test page: the tree without the overlay.
  window.ptParsePrompt = parsePrompt;
})();
</script>
"""
