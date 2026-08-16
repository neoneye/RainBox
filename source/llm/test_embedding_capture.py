"""Tests for the embedding-capture tally in `llm`: `capture_embeddings`
collects every embedding call made inside the block — which model, how long,
how much text — wherever in the retrieval stack it happens.

Capturing through instrumentation rather than a wrapper is the point: the
embedder is reached from claim vector search, the seed KB's populate, and its
retrieve, and the call nobody remembered to wrap is the one worth seeing.

Pure unit tests — events are constructed directly, no provider or DB.
"""

from llama_index.core.instrumentation import get_dispatcher
from llama_index.core.instrumentation.events.embedding import (
    EmbeddingEndEvent,
    EmbeddingStartEvent,
)

from llm import EMBEDDED_TEXT_CHARS, EMBEDDED_TEXTS_KEPT, capture_embeddings

MODEL = "embeddinggemma:300m"


def _embed(texts: list[str], model: str = MODEL) -> None:
    """One embedding call as the instrumentation sees it."""
    dispatcher = get_dispatcher()
    dispatcher.event(EmbeddingStartEvent(model_dict={"model_name": model}))
    dispatcher.event(EmbeddingEndEvent(
        chunks=texts, embeddings=[[0.1, 0.2] for _ in texts]))


def test_each_call_is_timed_and_named():
    with capture_embeddings() as tally:
        _embed(["what languages do I know"])

    assert tally.count == 1
    call = tally.calls[0]
    assert call["model"] == MODEL
    assert call["texts"] == 1
    assert call["chars"] == len("what languages do I know")
    assert call["ms"] is not None
    assert call["requested_at"]                  # placeable on the run's clock
    assert call["preview"] == ["what languages do I know"]


def test_the_text_that_went_in_is_kept():
    """What was embedded, not just how much of it: two queries of the same
    length are indistinguishable by `chars`, and telling them apart is the
    whole reason to look at a row of embed calls."""
    with capture_embeddings() as tally:
        _embed(["aardvark"])
        _embed(["windmill"])                     # same length, different query

    assert [c["preview"] for c in tally.calls] == [["aardvark"], ["windmill"]]


def test_a_bulk_call_is_previewed_not_transcribed():
    """A first-run seed populate embeds the whole registry in one call. The
    preview is bounded in both directions — a few texts, each cut short — so
    the payload stays a diagnostic and not a copy of the corpus."""
    long_text = "x" * (EMBEDDED_TEXT_CHARS + 50)
    with capture_embeddings() as tally:
        _embed([long_text] * (EMBEDDED_TEXTS_KEPT + 4))

    call = tally.calls[0]
    assert call["texts"] == EMBEDDED_TEXTS_KEPT + 4   # counted in full
    assert call["chars"] == len(long_text) * (EMBEDDED_TEXTS_KEPT + 4)
    assert len(call["preview"]) == EMBEDDED_TEXTS_KEPT
    assert all(len(t) == EMBEDDED_TEXT_CHARS for t in call["preview"])


def test_totals_cover_every_call_including_a_bulk_one():
    """A first-run seed populate embeds its whole registry in one batched
    call; the totals have to count the chunks, not the calls."""
    with capture_embeddings() as tally:
        _embed(["one"])
        _embed(["two", "three", "four"])

    assert tally.count == 2
    assert tally.total_chars == len("onetwothreefour")
    assert tally.total_ms >= 0


def test_capture_stops_at_the_block_boundary():
    """The handler is removed on exit — an embed belonging to the next step
    must not land in the step that already settled."""
    with capture_embeddings() as tally:
        _embed(["inside"])
    _embed(["outside"])

    assert tally.count == 1
    assert tally.calls[0]["chars"] == len("inside")


def test_an_end_with_no_start_still_counts_as_a_call():
    """Entering mid-call loses the duration, never the fact that the embedder
    ran: a call dropped from the tally is time the trace cannot explain."""
    dispatcher = get_dispatcher()
    with capture_embeddings() as tally:
        dispatcher.event(EmbeddingEndEvent(chunks=["orphan"], embeddings=[[0.1]]))

    assert tally.count == 1
    assert tally.calls[0]["ms"] is None
    assert tally.calls[0]["model"] is None
