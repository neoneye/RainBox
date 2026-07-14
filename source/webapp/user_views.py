from uuid import UUID

from flask import render_template_string, request

from agents.config import agent_config
from db import get_chat_user

from .core import app


# Identity-only for now (name / type / purpose / uuid); the card is structured so
# more sections (activity, rooms, …) can be added later without reshaping it.
USER_TEMPLATE: str = """
<!doctype html>
<title>{% if user %}{{ user.name }}{% else %}User{% endif %} &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0}
  .pp-content{max-width:680px}
  .ucard{border:1px solid #e5e7eb;border-radius:8px;background:#fff;
         box-shadow:0 1px 2px rgba(0,0,0,0.05);overflow:hidden;margin-top:0.5rem}
  .ucard .hd{padding:12px 16px;background:#fbfdff;border-bottom:1px solid #e5e7eb;
             display:flex;gap:0.5rem;align-items:baseline}
  .ucard .hd h1{margin:0;font-size:1.25rem}
  .ucard .utype{font-size:0.78rem;color:#475467;background:#eef2ff;
                border-radius:999px;padding:1px 8px}
  .ucard dl{margin:0;padding:14px 16px;display:grid;grid-template-columns:7rem 1fr;
            gap:0.5rem 1rem}
  .ucard dt{color:#6b7280;font-size:0.85rem}
  .ucard dd{margin:0}
  .ucard code{background:#eee;padding:1px 4px;border-radius:3px;
              font-family:ui-monospace,monospace;font-size:90%}
  .muted{color:#98a2b3}
</style>
{% include "_nav.html" %}
<div class="pp-content">
{% if user %}
  <div class="ucard">
    <div class="hd">
      <h1>{{ user.name }}</h1>
      <span class="utype">{{ user.user_type }}</span>
    </div>
    <dl>
      <dt>Purpose</dt><dd>{% if purpose %}{{ purpose }}{% else %}<span class="muted">&mdash;</span>{% endif %}</dd>
      <dt>UUID</dt><dd><code>{{ user.uuid }}</code></dd>
    </dl>
  </div>
{% else %}
  <div class="ucard">
    <div class="hd"><h1>User not found</h1></div>
    <dl><dt>Requested</dt><dd><code>{{ requested or '—' }}</code></dd></dl>
  </div>
{% endif %}
</div>
"""


def _purpose_for(user) -> str:
    """A one-line description of what this participant is for. The single human is
    the operator; each agent reuses its agent_config uuid, so we look its purpose
    up there (None when an agent has no config match, e.g. a retired role)."""
    if user.user_type == "human":
        return "Operator — the human running rainbox."
    entry = next(
        (e for e in agent_config.values() if e["uuid"] == user.uuid), None
    )
    return entry["description"] if entry else ""


@app.route("/user")
def user_page() -> str:
    """Identity card for a chat participant (the human operator or an agent),
    addressed by uuid via ?id= (consistent with /assistant, /chat, /model)."""
    requested = request.args.get("id")
    user = None
    if requested:
        try:
            user = get_chat_user(UUID(requested))
        except ValueError:
            user = None
    purpose = _purpose_for(user) if user is not None else ""
    return render_template_string(
        USER_TEMPLATE, user=user, purpose=purpose, requested=requested
    )
