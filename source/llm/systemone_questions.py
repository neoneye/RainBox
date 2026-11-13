"""The questions production asks of the decision models, each defined once.

A question's text is what the model scores the state against, so a change of
wording is a change of behaviour: bump the question's `version`, and the
set's when a caller's set changes. No example phrases in any of them.
"""
from llm.systemone import Bands, Question

#: Asked of one exchange and the human message after it. The state is
#: `{request, reply, next_message}`.
#:
#: Its band is the midpoint, not the default 0.8: measured on one room of 70
#: follow-ups with `d1:3b`, 0.5 caught 14 of 19 follow-ups that asked again
#: and called 1 of 43 others one. The same model asked whether the reply
#: "did not do what was asked" separated the two no better than chance, so
#: the question names the one thing it reads well.
FOLLOW_UP_RETRY = Question(
    name="retry",
    kind="noul",
    instructions="The next message asks for the same thing as the request once more.",
    version=1,
    bands=Bands(yes=0.5, no=0.5),
)

FOLLOW_UP_SET_VERSION = 1
