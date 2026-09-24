"""Diary vectors with a fake embedder (no live model in pytest): content-keyed
storage, resumable backfill, rejection and retries, epoch changes, stale-work
discard, sweeps, exact vs HNSW routes, outage fallback, and the probe."""

import hashlib
import math
import re
from datetime import date

import pytest
import sqlalchemy as sa

import db
from diary import embeddings as emb
from diary.action import diary_query
from diary.probe import release_gate, run_probe
from diary.test_retrieval import BUDGET, app_ctx, live, world  # noqa: F401 — fixtures

WORD = re.compile(r"[\wäöüæøåÄÖÜÆØÅ]+")


def bow(text: str) -> list[float]:
    """Deterministic bag-of-words vector: similar words, similar vectors."""
    v = [0.0] * emb.EMBED_DIM
    for w in WORD.findall(text.lower()):
        for gram in {w[i:i + 4] for i in range(max(1, len(w) - 3))}:
            h = int(hashlib.sha256(gram.encode()).hexdigest(), 16)
            v[h % emb.EMBED_DIM] += 1.0
    if not any(v):
        v[0] = 1.0
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


class FakeEmbedder:
    def __init__(self, digest="fakedigest000000", fail_times=0, bad=None, on_embed=None):
        self._digest, self.fail_times, self.bad, self.on_embed = digest, fail_times, bad, on_embed
        self.calls = 0
        self.inputs: list[str] = []

    def digest(self):
        return self._digest

    def embed(self, texts, *, timeout):
        self.calls += 1
        if self.on_embed:
            self.on_embed()
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TimeoutError("simulated")
        self.inputs += texts
        out = [bow(t) for t in texts]
        if self.bad is not None:
            out[0] = self.bad
        return out


def spec(fmt=1, digest="fakedigest000000"):
    return {"base_url": "http://127.0.0.1:11434", "model": "embeddinggemma:300m",
            "digest": digest, "dim": emb.EMBED_DIM, "input_format": fmt}


def fake_query(fail=False):
    def embed_query(text, sp):
        if fail:
            raise TimeoutError("model outage")
        return bow(emb.format_query(text, sp["input_format"]))
    return embed_query


def vector_rows(src_uuid):
    return db.session.execute(sa.text(
        "SELECT model_epoch, text_hash FROM diary_embedding WHERE source_uuid = :s"),
        {"s": src_uuid}).all()


def distinct_hashes(src_uuid):
    return {h for h, _ in emb._current_hashes(src_uuid)}


# --- adapter -------------------------------------------------------------------------------


@pytest.mark.parametrize("url, ok", [
    ("http://127.0.0.1:11434", True), ("http://localhost:11434", True),
    ("http://[::1]:11434", True), ("http://10.0.0.5:11434", False),
    ("https://embeddings.example.org", False),
])
def test_loopback_only(url, ok):
    assert emb.is_loopback_url(url) is ok
    if not ok:
        with pytest.raises(emb.EmbeddingError):
            emb.OllamaEmbedder(url)


@pytest.mark.parametrize("vec, ok", [
    ([0.1] * 768, True), ([0.0] * 768, False), ([0.1] * 767, False),
    ([float("nan")] + [0.1] * 767, False), ([float("inf")] + [0.1] * 767, False),
])
def test_vector_validation(vec, ok):
    assert emb.valid_vector(vec) is ok


def test_input_formats():
    assert emb.format_document("x", 1) == "x" and emb.format_query("x", 1) == "x"
    assert emb.format_document("x", 2) == "title: none | text: x"
    assert emb.format_query("x", 2) == "task: search result | query: x"
    assert emb.epoch_of(spec(1)) != emb.epoch_of(spec(2))


# --- backfill --------------------------------------------------------------------------------


def test_backfill_is_content_keyed_and_resumable(live):
    fake = FakeEmbedder()
    r = emb.embed_source(live.src.uuid, fake, spec())
    hashes = distinct_hashes(live.src.uuid)
    assert r.embedded == len(hashes) == r.missing
    rows = vector_rows(live.src.uuid)
    assert len(rows) == len(hashes)                     # routine text shares one vector
    again = emb.embed_source(live.src.uuid, FakeEmbedder(), spec())
    assert (again.missing, again.embedded) == (0, 0)


def test_append_embeds_only_new_text(live):
    emb.embed_source(live.src.uuid, FakeEmbedder(), spec())
    path = live.root / "current" / "2027.txt"
    path.write_bytes(path.read_bytes() + b"\n20270315\n09h00\nbrand new sentence\n")
    live.resync()
    fake = FakeEmbedder()
    r = emb.embed_source(live.src.uuid, fake, spec())
    assert r.embedded == 1 and fake.inputs == ["09h00\nbrand new sentence\n"]


def test_invalid_vectors_rejected_and_retries(live):
    r = emb.embed_source(live.src.uuid, FakeEmbedder(bad=[0.0] * 768), spec())
    assert r.rejected_vectors >= 1
    retried = FakeEmbedder(fail_times=2)
    ok = emb.embed_source(live.src.uuid, retried, spec())
    assert ok.failed_batches == 0 and ok.embedded >= 1
    broken = emb.embed_source(live.src.uuid, FakeEmbedder(fail_times=99), spec(fmt=2))
    assert broken.failed_batches == broken.batches and broken.embedded == 0


def test_spec_change_turns_vectors_off_and_sweeps_old_epoch(live):
    emb.embed_source(live.src.uuid, FakeEmbedder(), spec(1))
    emb.set_vector_mode(live.src.uuid, "exact")
    emb.embed_source(live.src.uuid, FakeEmbedder(), spec(2))
    src = db.diary_get_source(live.src.uuid)
    assert src.vector_mode == "off"
    assert {e for e, _ in vector_rows(live.src.uuid)} == {emb.epoch_of(spec(2))}


def test_policy_change_discards_in_flight_work(live):
    def bump():
        db.diary_exclude(live.src.uuid, "archive/ChangeLog.txt")
    r = emb.embed_source(live.src.uuid, FakeEmbedder(on_embed=bump), spec())
    assert r.aborted == "policy_changed" and r.embedded == 0


def test_sweep_removes_vectors_of_text_edited_out(live):
    emb.embed_source(live.src.uuid, FakeEmbedder(), spec())
    gone = hashlib.sha256("13h15\nMein Rücken fühlt sich wieder normal an.\n".encode()).hexdigest()
    assert gone in {h for _, h in vector_rows(live.src.uuid)}
    path = live.root / "current" / "2027.txt"
    path.write_bytes(path.read_bytes().replace("Mein Rücken fühlt sich wieder normal an.\n".encode(), b""))
    live.resync()
    emb.embed_source(live.src.uuid, FakeEmbedder(), spec())
    assert gone not in {h for _, h in vector_rows(live.src.uuid)}


def test_vector_mode_preconditions(live):
    with pytest.raises(db.DiaryError, match="epoch"):
        emb.set_vector_mode(live.src.uuid, "exact")
    emb.embed_source(live.src.uuid, FakeEmbedder(), spec())
    with pytest.raises(db.DiaryError, match="passing"):
        emb.set_vector_mode(live.src.uuid, "hnsw")
    assert emb.set_vector_mode(live.src.uuid, "exact").vector_mode == "exact"


# --- query side ---------------------------------------------------------------------------------


def _vectors_on(live):
    emb.embed_source(live.src.uuid, FakeEmbedder(), spec())
    emb.set_vector_mode(live.src.uuid, "exact")


def test_vector_route_finds_paraphrase_lexical_misses(live):
    _vectors_on(live)
    ctx = live.ctx()
    lexical = diary_query({"mode": "search", "query": "physiotherapie"}, ctx, budget=BUDGET)
    assert "Physiotherapy" not in lexical.text                      # no vectors passed
    fused = diary_query({"mode": "search", "query": "physiotherapie"}, ctx, budget=BUDGET,
                        embed_query=fake_query())
    assert "Physiotherapy" in fused.text


def test_outage_keeps_lexical_results(live):
    _vectors_on(live)
    obs = diary_query({"mode": "search", "query": "physiotherapy"}, live.ctx(), budget=BUDGET,
                      embed_query=fake_query(fail=True))
    assert obs.ok and "Physiotherapy" in obs.text
    assert "vector_failed" in obs.data["degraded_routes"]


def test_digest_mismatch_disables_vector_route(live):
    _vectors_on(live)
    qe = emb.QueryEmbedder(factory=lambda sp: FakeEmbedder(digest="differentdigest0"))
    obs = diary_query({"mode": "search", "query": "physiotherapy"}, live.ctx(), budget=BUDGET,
                      embed_query=qe)
    assert "vector_failed" in obs.data["degraded_routes"]
    assert "Physiotherapy" in obs.text


def test_exact_mode_ignores_an_existing_hnsw_index(live):
    _vectors_on(live)
    from diary.retrieval import DiaryRequest, _date_sql, _params, eligible_sources
    sources = eligible_sources(live.ctx())
    req = DiaryRequest(mode="search", query="undo robustness exceptions")
    before = emb.vector_route(req, sources, fake_query(), _params(sources, req), _date_sql(req),
                              mode_override="exact")
    emb.build_hnsw_index()
    after = emb.vector_route(req, sources, fake_query(), _params(sources, req), _date_sql(req),
                             mode_override="exact")
    assert before == after and before


def test_hnsw_fills_under_restrictive_filter(live):
    _vectors_on(live)
    emb.build_hnsw_index()
    from diary.retrieval import DiaryRequest, _date_sql, _params, eligible_sources
    sources = eligible_sources(live.ctx())
    req = DiaryRequest(mode="search", query="undo", date_from=date(2027, 7, 26), date_to=date(2027, 7, 26))
    ids = emb.vector_route(req, sources, fake_query(), _params(sources, req), _date_sql(req),
                           mode_override="hnsw")
    # Every eligible passage that day (one ChangeLog bullet, two daily
    # entries), although the index alone could stop short after filtering.
    assert len(ids) == 3


# --- probe --------------------------------------------------------------------------------------


def _gold(live, path, needle):
    raw = (live.root / path).read_bytes()
    start = raw.index(needle.encode())
    return {"path": path, "byte_start": start, "byte_end": start + len(needle.encode())}


def test_probe_scores_and_gates(live):
    cases = [
        {"id": "lit-1", "kind": "literal", "args": {"mode": "literal", "query": "Edit::VSpace#move_left"},
         "gold": [_gold(live, "archive/ChangeLog.txt", "Edit::VSpace#move_left")]},
        {"id": "top-1", "kind": "topical", "args": {"mode": "search", "query": "back pain Rücken"},
         "gold": [_gold(live, "current/2027.txt", "Mein Rücken")]},
        {"id": "tl-1", "kind": "timeline",
         "args": {"mode": "timeline", "date_from": "2027-07-26", "date_to": "2027-07-26"},
         "gold": [_gold(live, "archive/ChangeLog.txt", "*\tre-enabled Buffer#test_exception_xxx.\n"),
                  _gold(live, "daily/2027_07_26.txt", "0830"), _gold(live, "daily/2027_07_26.txt", "0910")]},
        {"id": "neg-1", "kind": "negative", "args": {"mode": "literal", "query": "zeppelin-xyz"}, "gold": []},
    ]
    db.diary_set_enabled(live.src.uuid, False)          # the probe reads disabled sources
    report = run_probe(db.diary_get_source(live.src.uuid), cases, timing_passes=1)
    assert all(r["correct"] for r in report["cases"]), report["cases"]
    assert report["gate"]["passed"]
    assert "text" not in str(report["cases"])
    assert set(report["latency"]) == {"literal", "search", "timeline"}


def test_release_gate_counts():
    rs = [{"kind": "topical", "correct": i < 6} for i in range(8)]
    assert release_gate(rs)["failed"] == ["topical"]
    rs = [{"kind": "topical", "correct": i < 7} for i in range(8)]
    assert release_gate(rs)["passed"]
