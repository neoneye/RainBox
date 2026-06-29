"""Deterministic belief keying — no LLM on the write path."""
import pytest
import db
from db.memory import belief_keys, KEY_VERSION, normalize_claim_text

SEP = "\x1f"


def test_explicit_subject_predicate_used_verbatim():
    sp, val = belief_keys("Alice", "prefers", "tea", "Alice prefers tea")
    assert sp == normalize_claim_text("Alice") + SEP + normalize_claim_text("prefers")
    assert val == normalize_claim_text("tea")


@pytest.mark.parametrize("text,subj,pred,val", [
    ("Alice is happy", "alice", "is", "happy"),
    ("Bob prefers tea", "bob", "prefers", "tea"),
    ("Carol uses vim", "carol", "uses", "vim"),
])
def test_parses_common_shapes(text, subj, pred, val):
    sp, value = belief_keys(None, None, None, text)
    assert sp == subj + SEP + pred
    assert value == val


def test_free_text_has_empty_subj_pred_key():
    sp, val = belief_keys(None, None, None, "we discussed the roadmap yesterday")
    assert sp == ""
    assert val == normalize_claim_text("we discussed the roadmap yesterday")


def test_key_version_is_one():
    assert KEY_VERSION == 1
