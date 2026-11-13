"""The questions production asks of the decision models, each defined once.

A question's text is what the model scores the state against, so a change of
wording is a change of behaviour: bump the question's `version`, and the
set's when a caller's set changes.
"""
from llm.systemone import Question

FOLLOW_UP_SET_VERSION = 4

#: The option of `FOLLOW_UP_REPEAT` that says the follow-up does not ask
#: again. The other options are the ways in which it does.
FOLLOW_UP_DIFFERENT = "different"
#: A follow-up asks again when the options other than `different` hold at
#: least this much of the probability between them.
FOLLOW_UP_AGAIN_AT = 0.5

#: Asked of two human messages, a request and the human's next message, as
#: `follow_up_state` writes them.
#:
#: Measured on one room with `d1:3b`, on 16 follow-ups that asked again and
#: 41 that did not (labelled by hand before the run): at 0.5 it caught 14
#: and called none of the others one. With the assistant's reply between
#: the two it caught 15 and called 1, so the reply adds nothing that can be
#: told from one run, and it is left out.
FOLLOW_UP_REPEAT = Question(
    name="repeat",
    kind="choice",
    instructions="Does message2 ask again for what message1 asked for?",
    version=3,
    options={
        "duplicate": "Yes, in the same or almost the same words.",
        "same": "Yes, in different words, or by asking for another attempt.",
        FOLLOW_UP_DIFFERENT: "No, it asks for something else.",
    },
)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def follow_up_state(request: str, next_message: str) -> str:
    """The two human messages as the decision model is given them: each
    inside a tag of its own, named as the question names them. Angle
    brackets in a message are escaped, so a message cannot close its tag
    and open another.

    The assistant's reply is not given. It can carry text from elsewhere,
    and left out it cannot steer the answer at all. The tags mark where each
    message begins and ends; they do not make the model deaf to what a
    message says. Measured with `d1:3b`, a sentence planted in the next
    message, saying that it repeats the request, moved nearly every
    unrelated follow-up to "asks again", with tags as without. That is the
    human's own message, and the answer is about it."""
    return (f"<message1>{_escape(request)}</message1>\n"
            f"<message2>{_escape(next_message)}</message2>")


def follow_up_again(probabilities: dict[str, float]) -> tuple[bool, float, str]:
    """`(asks again, probability of it, the likelier way)` from the answer
    to `FOLLOW_UP_REPEAT`."""
    again = {k: v for k, v in probabilities.items() if k != FOLLOW_UP_DIFFERENT}
    p = sum(again.values())
    return p >= FOLLOW_UP_AGAIN_AT, p, max(again, key=lambda k: again[k])
