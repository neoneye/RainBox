"""Follow-up classifier agent — a specialized StructuredLLMAgent.

Specialized agents live in their own `agent_<purpose>.py` module; the shared
base classes (Agent, ModelGroupAgent, StructuredLLMAgent) stay in agent.py.
"""

import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from agent import StatusSender, StructuredLLMAgent


class FollowUpClassification(BaseModel):
    needs_response: Literal["yes", "maybe", "no"] = Field(
        description='Whether the message needs a follow-up reply: "yes", "maybe", or "no".'
    )


FOLLOWUP_SYSTEM_PROMPT: str = """\
You classify a SINGLE chat message in isolation (no conversation history) \
and decide whether it needs a follow-up response from someone.

You MUST respond with a single JSON object that strictly adheres to the \
`FollowUpClassification` schema. The schema has exactly one field:
  - `needs_response` (string): one of "yes", "maybe", or "no".

Guidance:
  - "yes": the message asks a question, makes a request, or clearly \
expects a reply or action. e.g. "Can someone check the benchmark logs?"
  - "no": a closing remark, acknowledgement, or informational/system \
notice that does not invite a reply. e.g. "Thanks!", "Channel created."
  - "maybe": ambiguous on its own and might need a reply depending on \
context. e.g. a bare "yes".

Rules:
  - Output the JSON object and nothing else — no prose, no markdown \
fences, no leading or trailing text.
  - Do not add any fields beyond `needs_response`.
  - `needs_response` must be exactly one of the three lowercase words."""


class FollowUpClassifierAgent(StructuredLLMAgent):
    """Classifies a single chat message (no history) as needing a follow-up
    response: `needs_response` ∈ {"yes", "maybe", "no"}.

    Supplies its own system prompt and response model, so callers pass only the
    usual (agent_uuid, name, send). The message to classify is taken from the
    payload's `message`/`text`/`prompt` field (else the whole payload as JSON).
    """

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(
            agent_uuid,
            name,
            send,
            system_prompt=FOLLOWUP_SYSTEM_PROMPT,
            response_model=FollowUpClassification,
        )

    def user_prompt(self, payload: dict[str, Any]) -> str:
        for key in ("message", "text", "prompt"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(payload)
