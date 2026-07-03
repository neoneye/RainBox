"""`_build_documents` must embed the QUESTION only, never the answer.

The answer is carried as node metadata (so retrieval can return it), but
LlamaIndex folds metadata into the embedded text by default AND guards it
against the chunk size before embedding: a long answer as metadata trips
"Metadata length (N) is longer than chunk size (1024)". Excluding the metadata
from the embed/LLM text both fixes that crash and makes embeddings truly
question-only. This test drives the real SentenceSplitter path that
VectorStoreIndex.from_documents runs, so it reproduces the /settings crash.
"""
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import MetadataMode

import memory.seed_memory as seed_memory


def test_long_answer_does_not_trip_chunk_size_guard():
    # Answer far longer than the default 1024-token chunk size (the /settings case).
    long_answer = "sentence about the topic. " * 400
    entries = [{"id": "big", "kind": "static", "questions": ["what is it?"],
                "answer": long_answer}]

    docs = seed_memory._build_documents(entries)

    # This is what VectorStoreIndex.from_documents does internally; before the
    # fix it raises ValueError("Metadata length ... longer than chunk size").
    SentenceSplitter().get_nodes_from_documents(docs)


def test_answer_is_excluded_from_embedded_text():
    entries = [{"id": "s", "kind": "static", "questions": ["what is it?"],
                "answer": "the secret answer"}]

    doc = seed_memory._build_documents(entries)[0]

    embedded = doc.get_content(metadata_mode=MetadataMode.EMBED)
    assert "the secret answer" not in embedded, (
        f"answer must not be part of the embedded text: {embedded!r}"
    )
    assert embedded.strip() == "what is it?"
    # The answer is still available as metadata for retrieval/_resolve_match.
    assert doc.metadata["answer"] == "the secret answer"


def test_shield_present_is_metadata_and_excluded_from_embed():
    entries = [{"id": "s", "kind": "static", "questions": ["what is it?"],
                "answer": "a", "shield": "alice.travel"}]
    doc = seed_memory._build_documents(entries)[0]
    assert doc.metadata["shield"] == "alice.travel"
    # Excluded from the embedded/LLM text, like the other metadata keys.
    assert "shield" in doc.excluded_embed_metadata_keys
    assert "shield" in doc.excluded_llm_metadata_keys


def test_no_shield_means_no_shield_metadata_key():
    entries = [{"id": "s", "kind": "static", "questions": ["what is it?"],
                "answer": "a"}]
    doc = seed_memory._build_documents(entries)[0]
    assert "shield" not in doc.metadata
