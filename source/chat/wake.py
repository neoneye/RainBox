"""Wake-ups: turns the assistant starts without a message from the user.

A wake-up is a room row of kind `wake`. It is the turn's trigger, carried in
the inbox payload as `message_uuid` like a user's message, and for that one
turn the assistant reads its text where a request would be. It is not a
`message` row, so nothing that reads a room's conversation (the history
window, the summaries, the room's topics) ever sees it.

The row's text is the notice the model reads: what woke it and what the
wake-up is for. `meta["wake"]` holds the same facts for code: `source`, and
whatever that source records about itself. A new source — a timer, an event —
is a new constant here and a notice of its own; the assistant's prompts
describe wake-ups in general and take the rest from the notice.
"""

WAKE_KIND = "wake"

# The Continue button in the chat's Settings sidebar.
CONTINUE_BUTTON = "continue_button"


def continue_notice(index: int, count: int) -> str:
    """The notice for wake-up `index` of the `count` one press of Continue
    asked for."""
    notice = (
        "Wake-up notice: the user pressed the Continue button and wrote no "
        "message. Carry on from your latest message.")
    if count > 1:
        notice += (
            f" This is wake-up {index} of {count} from that one press.")
    return notice


def continue_meta(index: int, count: int) -> dict:
    return {"wake": {"source": CONTINUE_BUTTON, "index": index, "count": count}}
