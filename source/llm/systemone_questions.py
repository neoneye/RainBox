"""The questions production asks of the decision models, each defined once.

A question's text is what the model scores the state against, so a change of
wording is a change of behaviour: bump the question's `version`, and the
set's when a caller's set changes.
"""
from llm.systemone import Question

FOLLOW_UP_SET_VERSION = 2

#: The option of `FOLLOW_UP_REPEAT` that says the follow-up does not ask
#: again. The other options are the ways in which it does.
FOLLOW_UP_DIFFERENT = "different"
#: A follow-up asks again when the options other than `different` hold at
#: least this much of the probability between them.
FOLLOW_UP_AGAIN_AT = 0.5

#: Asked of one exchange and the human message after it, as
#: `follow_up_state` writes them.
#:
#: Measured on one room with `d1:3b`, on 15 follow-ups that asked again and
#: 43 that did not (labelled by hand before the run): at 0.5 it caught 14
#: and called 1 of the others one. A yes-or-no question over an object with
#: the keys request, reply and next_message caught 12 and called none.
FOLLOW_UP_REPEAT = Question(
    name="repeat",
    kind="choice",
    instructions="Does request2 repeat request1?",
    version=1,
    options={
        "duplicate": "near duplicate, is it the same or rephrased in a slightly different way",
        "same": "same but expressed differently, such as 'try again'",
        FOLLOW_UP_DIFFERENT: "The request2 is different from request1",
    },
)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def follow_up_state(request: str, reply: str, next_message: str) -> str:
    """The three messages as the decision model is given them: each inside
    a tag of its own, named as the question names them. Angle brackets in a
    message are escaped, so a message cannot close its tag and open another.

    The tags mark where each message begins and ends. They do not make the
    model deaf to what a message says: measured with `d1:3b`, a sentence
    planted in the next message, saying that it repeats the request, moved
    42 of 43 unrelated follow-ups to "asks again", with tags as without.
    Planted in the reply it moved 2 of 43. So the reply, which can carry
    text from elsewhere, steers the answer little, and the human's own next
    message steers it fully, as it should."""
    return (f"<request1>{_escape(request)}</request1>\n"
            f"<reply1>{_escape(reply)}</reply1>\n"
            f"<request2>{_escape(next_message)}</request2>")


def follow_up_again(probabilities: dict[str, float]) -> tuple[bool, float, str]:
    """`(asks again, probability of it, the likelier way)` from the answer
    to `FOLLOW_UP_REPEAT`."""
    again = {k: v for k, v in probabilities.items() if k != FOLLOW_UP_DIFFERENT}
    p = sum(again.values())
    return p >= FOLLOW_UP_AGAIN_AT, p, max(again, key=lambda k: again[k])
