"""The /doctor page — `rainbox doctor` in the browser.

Renders `tools.doctor.run_doctor()` as a per-check table plus a copy-paste-able
plain-text report, so the operator never has to drop to a shell/SSH to see
whether the environment is healthy. Re-runs on every page load (the embedder
probe makes one fast-fail connect attempt). Read-only — no state is changed.
"""

from flask import render_template_string

from .core import app

DOCTOR_TEMPLATE = """
<!doctype html>
<title>Doctor &mdash; rainbox</title>
{% include "_nav.html" %}
<style>
  body { margin: 0; font-family: system-ui, sans-serif; }
  .pp-doc { max-width: 900px; margin: 1rem auto; padding: 0 1rem;
            font-family: system-ui, sans-serif; }
  .pp-doc h1 { margin: 0.2rem 0; }
  .pp-doc .summary { font-weight: 600; padding: 0.5rem 0.75rem; border-radius: 6px;
                     display: inline-block; }
  .pp-doc .summary.ok   { background: #e6f4ea; color: #1e7e34; }
  .pp-doc .summary.warn { background: #fff4e5; color: #b06f00; }
  .pp-doc .summary.fail { background: #fdecea; color: #c0392b; }
  .pp-doc table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
  .pp-doc td { padding: 0.5rem 0.6rem; border-bottom: 1px solid #eee;
               vertical-align: top; }
  .pp-doc td.icon { width: 1.5rem; font-size: 1.1rem; text-align: center; }
  .pp-doc td.name { width: 9rem; font-weight: 600; }
  .pp-doc tr.status-ok   td.icon { color: #1e7e34; }
  .pp-doc tr.status-warn td.icon { color: #b06f00; }
  .pp-doc tr.status-fail td.icon { color: #c0392b; }
  .pp-doc .detail { color: #333; word-break: break-word; }
  .pp-doc .bar { display: flex; gap: 0.75rem; align-items: center; margin: 0.5rem 0; }
  .pp-doc pre { background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 6px;
                padding: 0.75rem; overflow-x: auto; white-space: pre-wrap; }
  .pp-doc button, .pp-doc a.btn { font: inherit; padding: 0.35rem 0.7rem;
                cursor: pointer; border: 1px solid #ccc; border-radius: 6px;
                background: #fff; text-decoration: none; color: #222; }
</style>
<main class="pp-doc">
  <h1>Doctor</h1>
  <div class="bar">
    <span class="summary {{ overall }}">{{ summary }}</span>
    <a class="btn" href="{{ url_for('doctor_page') }}">Re-run</a>
  </div>
  <table>
    {% for c in checks %}
    <tr class="status-{{ c.status }}">
      <td class="icon">{{ icons[c.status] }}</td>
      <td class="name">{{ c.name }}</td>
      <td class="detail">{{ c.detail }}</td>
    </tr>
    {% endfor %}
  </table>
  <div class="bar">
    <strong>Plain text</strong>
    <button type="button" onclick="ppCopyDoctor(this)">Copy</button>
  </div>
  <pre id="pp-doctor-report">{{ report }}</pre>
</main>
<script>
  // document.execCommand returning true is not proof of a copy: the call can be
  // proxied by an extension that returns true and copies nothing. The browser's
  // own `copy` event fires only on a real copy, so that is what is watched for.
  // Returns 'ok', 'blocked', or 'unavailable' (no execCommand, no focus, or no
  // user gesture left).
  function ppVerifiedCopy(text) {
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
  // The label says what happened. A button that says "Copied" over a clipboard
  // that was never written pastes the previous report into a bug thread as if
  // it were this one.
  function ppCopyDoctor(btn) {
    var t = document.getElementById('pp-doctor-report').innerText;
    var old = btn.textContent;
    var say = function (ok) {
      btn.textContent = ok ? 'Copied' : 'Copy failed';
      setTimeout(function () { btn.textContent = old; }, 1200);
    };
    // The verified path first: the Clipboard API resolves the same whether the
    // write landed or was intercepted, so it cannot confirm anything.
    var status = ppVerifiedCopy(t);
    if (status !== 'unavailable') { say(status === 'ok'); return; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(
        function () { say(true); }, function () { say(false); });
    } else {
      say(false);
    }
  }
</script>
"""

_ICONS = {"ok": "✓", "warn": "!", "fail": "✗"}


@app.route("/doctor")
def doctor_page() -> str:
    from tools.doctor import format_checks, run_doctor

    checks = run_doctor()
    n_fail = sum(1 for c in checks if c.status == "fail")
    n_warn = sum(1 for c in checks if c.status == "warn")
    if n_fail:
        overall, summary = "fail", f"{n_fail} failing, {n_warn} warning"
    elif n_warn:
        overall, summary = "warn", f"All usable; {n_warn} warning(s)"
    else:
        overall, summary = "ok", "All checks pass"
    return render_template_string(
        DOCTOR_TEMPLATE,
        checks=checks, icons=_ICONS, overall=overall, summary=summary,
        report="rainbox doctor\n" + format_checks(checks),
    )
