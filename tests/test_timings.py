"""M4 T5 — timings object + bench-mode strip.

Covers spec A8 (timings semantics + perf_counter source) and A10
(free-path byte-identity preserved via bench-mode strip).

CRITICAL INTERNAL ORDERING (per plan T5):
The `timings` field on search()'s return MUST land in the same commit
as the bench-strip patch in bench_longmemeval.py. Without the strip,
every byte of every bench JSONL line changes → A10 cmp fails.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


# Use the search_corpus_db helper from T4's test module.


@pytest.fixture
def search_corpus_db(tmp_path, monkeypatch):
    """Small DB injected as the sole project-scoped DB."""
    import sqlite3, sqlite_vec, uuid
    from sage_memory.db import _migrate

    docs = [
        ("Quintarius Ozymandias dossier",
         "Quintarius Ozymandias is the unique subject."),
        ("Brief note one", "Quintarius is briefly mentioned here."),
        ("Cartography essay", "An essay on map-making. " * 30),
    ]
    db_file = tmp_path / "search.db"
    conn = sqlite3.connect(str(db_file), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _migrate(conn)
    now = time.time()
    for title, content in docs:
        mid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO memories (id, title, content, tags, "
            "content_hash, created_at, updated_at, accessed_at) "
            "VALUES (?, ?, ?, '[]', ?, ?, ?, ?)",
            (mid, title, content, mid, now, now, now),
        )
    conn.commit()

    monkeypatch.setattr(
        "sage_memory.search.get_all_dbs",
        lambda: [("project", conn)],
    )
    yield conn
    conn.close()


# ─── A8: timings schema + source ──────────────────────────────────


_TIMINGS_KEYS = {
    "expansion_ms", "retrieval_ms", "fusion_ms", "dedup_ms",
    "rerank_ms", "scoring_ms", "total_ms",
}


def test_timings_present_on_every_result(search_corpus_db):
    """A8 schema: result dict always has `timings` with all 7 keys."""
    from sage_memory import search

    out = search.search(
        query="quintarius", strategy="keyword",
        expand=False, rerank=False,
    )
    assert "timings" in out, "search result must always have `timings`"
    assert set(out["timings"].keys()) == _TIMINGS_KEYS
    for k, v in out["timings"].items():
        assert isinstance(v, (int, float)), f"{k} must be numeric"


def test_timings_uses_perf_counter(search_corpus_db, monkeypatch):
    """A8 pinned source: perf_counter (not time.time)."""
    from sage_memory import search

    call_count = [0]
    # Inject a counter that returns a known sequence.
    real_perf = time.perf_counter

    def _counter():
        call_count[0] += 1
        return float(call_count[0])

    monkeypatch.setattr(time, "perf_counter", _counter)
    try:
        out = search.search(
            query="quintarius", strategy="keyword",
            expand=False, rerank=False,
        )
    finally:
        monkeypatch.setattr(time, "perf_counter", real_perf)

    # If timings uses perf_counter, total_ms will be (end-start)*1000
    # where end-start is at least 1 (per our counter increment per call).
    assert out["timings"]["total_ms"] >= 1.0, (
        "timings should use perf_counter; total_ms suspiciously small"
    )


def test_timings_inactive_stage_reports_zero(search_corpus_db):
    """A8: stages that didn't run report 0.0; field always present."""
    from sage_memory import search

    out = search.search(
        query="quintarius", strategy="keyword",
        expand=False, rerank=False,
    )
    # expansion_ms and rerank_ms are 0.0 when both stages skipped.
    assert out["timings"]["expansion_ms"] == 0.0
    assert out["timings"]["rerank_ms"] == 0.0


def test_timings_expansion_short_circuit_under_100ms(
    search_corpus_db, monkeypatch,
):
    """A8: when expand short-circuits, expansion_ms < 100ms (test gate)."""
    from sage_memory import search, expand as expand_mod

    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True,
    )
    # Force short-circuit so the LLM is never called.
    monkeypatch.setattr(
        expand_mod, "_is_strong_signal", lambda seed: True,
    )

    def _explode(*a, **kw):
        raise AssertionError("LLM should not be called on short-circuit")

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _explode,
    )

    out = search.search(
        query="quintarius", strategy="keyword",
        expand=True, rerank=False,
    )
    # short-circuit fires → expansion_ms is the probe + decision only.
    # 100ms test gate is well below any realistic LLM latency.
    assert out["timings"]["expansion_ms"] < 100.0


def test_timings_expansion_llm_call_over_100ms(
    search_corpus_db, monkeypatch,
):
    """A8: when expand calls the LLM, expansion_ms >= 100ms (mocked sleep)."""
    from sage_memory import search, expand as expand_mod

    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True,
    )
    # Force LLM-call path (bypass short-circuit decision).
    monkeypatch.setattr(
        expand_mod, "_is_strong_signal", lambda seed: False,
    )

    def _slow_expand(q):
        time.sleep(0.2)  # 200ms — well above the 100ms gate
        return {"lex": [q], "vec": q, "hyde": None}

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _slow_expand,
    )

    out = search.search(
        query="quintarius", strategy="keyword",
        expand=True, rerank=False,
    )
    assert out["timings"]["expansion_ms"] >= 100.0


# ─── A10: bench-strip ─────────────────────────────────────────────


def test_bench_strips_timings_before_jsonl_write():
    """A10: bench_longmemeval.py contains the timings-strip line.

    Smoke test by source inspection — verifies the strip patch is
    present and applied after each search() call. Without this line,
    A10 cmp byte-identity against M3b JSONLs would break the moment
    A4 lands `timings` on search()'s return.
    """
    bench_src = (
        Path(__file__).parent.parent
        / "evaluation" / "longmemeval" / "bench_longmemeval.py"
    ).read_text("utf-8")
    # The pop("timings", None) call is the strip; it must reference
    # the search result variable used in _ingest_and_search.
    assert "pop(\"timings\"" in bench_src or "pop('timings'" in bench_src, (
        "bench_longmemeval.py is missing the A10 timings-strip line. "
        "Add: `r.pop(\"timings\", None)` after every sage_search() call."
    )
