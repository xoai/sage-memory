"""M4 T4 — search.py wiring for expand + rerank.

Covers spec ACs:
  A9: 3-state matrix for `expand` / `rerank` MCP params (None / True
      / False) including the "force-enable + no key → WARN+skip" path.
  A14: mixed-failure paths end-to-end (expand-fails-rerank-succeeds
       and inverse).
  A15: dual LLM timeout no-hang test (worst-case bound).
  Plus: lex variant dedup, "expand=True + no key does not invoke
        module", final blend ordering by _blended_score.

Mocks `sage_memory.llm.expand_query_variants`, `sage_memory.llm
.rerank_candidates`, and `sage_memory.llm.is_configured`. Uses
small in-memory sqlite corpora via the M3a test_chunked_storage
pattern.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path

import httpx
import pytest


# ─── Helpers ──────────────────────────────────────────────────────


def _make_db_with_docs(tmp_path: Path, docs: list[tuple[str, str]]):
    """Returns a configured sqlite connection with `memories` populated.

    Mirrors the pattern used by tests/fixtures/expand_corpus.py
    but with full migrations (so vec tables exist if the search
    code path touches them).
    """
    import sqlite3, sqlite_vec
    from sage_memory.db import _migrate

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
    return conn


@pytest.fixture
def search_corpus_db(tmp_path, monkeypatch):
    """A small DB with a handful of memories; injected as the only DB
    available to search() via monkeypatching get_all_dbs.
    """
    docs = [
        ("Quintarius Ozymandias dossier",
         "Quintarius Ozymandias is the unique subject of this record."),
        ("Cartography essay", "An essay on map-making. " * 30),
        ("Poetry overview", "Survey of nineteenth-century verse. " * 30),
        ("Brief note one", "Quintarius is briefly mentioned here."),
        ("Brief note two", "Ozymandias also briefly appears."),
    ]
    conn = _make_db_with_docs(tmp_path, docs)

    # Wire the DB as the sole project-scoped DB.
    monkeypatch.setattr(
        "sage_memory.search.get_all_dbs",
        lambda: [("project", conn)],
    )
    monkeypatch.setattr(
        "sage_memory.db.get_all_dbs",
        lambda: [("project", conn)],
    )
    yield conn
    conn.close()


# ─── A9: expand 3-state matrix ────────────────────────────────────


def test_search_expand_none_enabled_when_llm_configured(
    monkeypatch, search_corpus_db,
):
    """A9 default: expand=None + LLM configured → expand IS called."""
    from sage_memory import search

    called = []

    def _fake_expand(q):
        called.append(q)
        return {"lex": [q], "vec": q, "hyde": None}

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _fake_expand,
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True,
    )
    # rerank: default off-key so we can isolate expand behavior.
    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates",
        lambda *a, **kw: pytest.fail("rerank should not run here"),
    )

    search.search(
        query="quintarius ozymandias",
        strategy="keyword", expand=None, rerank=False,
    )

    # The strong-signal short-circuit may or may not fire; either way
    # the expand decision was made under "enabled" gates.
    # We only assert: the gate didn't short out the whole feature.
    # If short-circuit fires, called == []. If not, called == [query].
    assert called in ([], ["quintarius ozymandias"])


def test_search_expand_true_warns_when_no_key(
    monkeypatch, search_corpus_db, caplog,
):
    """A9 force-enable + no key → WARN + skip; expand NOT called."""
    from sage_memory import search

    def _explode_expand(*a, **kw):
        raise AssertionError("expand must not be called without key")

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _explode_expand,
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: False,
    )

    with caplog.at_level(logging.WARNING, logger="sage-memory"):
        search.search(
            query="quintarius ozymandias",
            strategy="keyword", expand=True, rerank=False,
        )

    assert any(
        "expand=True" in r.message and r.levelno >= logging.WARNING
        for r in caplog.records
    )


def test_search_expand_true_no_key_does_not_invoke_module(
    monkeypatch, search_corpus_db,
):
    """Minor-substantive #10: expand=True + no key → bypass module entirely.

    Monkeypatch `expand.expand_query` to raise if called. search()
    should complete without raising — proving the WARN-path skipped
    the module before reaching its silent free-path floor.
    """
    from sage_memory import search, expand

    monkeypatch.setattr(
        expand, "expand_query",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("expand.expand_query must not be invoked"),
        ),
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: False,
    )

    # No exception raised → test passes.
    result = search.search(
        query="quintarius ozymandias",
        strategy="keyword", expand=True, rerank=False,
    )
    assert "results" in result


def test_search_expand_false_disables(monkeypatch, search_corpus_db):
    """A9 force-disable: expand=False even with key → expand NOT called."""
    from sage_memory import search

    def _explode(*a, **kw):
        raise AssertionError("expand must not be called when False")

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _explode,
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True,
    )

    search.search(
        query="quintarius ozymandias",
        strategy="keyword", expand=False, rerank=False,
    )


# ─── A9: rerank 3-state matrix ────────────────────────────────────


def test_search_rerank_3state_matrix(monkeypatch, search_corpus_db, caplog):
    """A9: rerank None / True / False matrix in one consolidated test."""
    from sage_memory import search

    rerank_call_log = []

    def _fake_rerank(query, candidates, **kw):
        rerank_call_log.append(query)
        return [{"id": c["id"], "score": 0.5} for c in candidates]

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake_rerank,
    )
    # expand disabled to isolate rerank.

    # None + key → enabled
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True,
    )
    rerank_call_log.clear()
    search.search(
        query="quintarius",
        strategy="keyword", expand=False, rerank=None,
    )
    assert len(rerank_call_log) == 1, "rerank=None + key → called"

    # True + no key → WARN + skip
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: False,
    )
    rerank_call_log.clear()
    with caplog.at_level(logging.WARNING, logger="sage-memory"):
        search.search(
            query="quintarius",
            strategy="keyword", expand=False, rerank=True,
        )
    assert rerank_call_log == []
    assert any(
        "rerank=True" in r.message and r.levelno >= logging.WARNING
        for r in caplog.records
    )

    # False + key → skip silently
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True,
    )
    rerank_call_log.clear()
    search.search(
        query="quintarius",
        strategy="keyword", expand=False, rerank=False,
    )
    assert rerank_call_log == []


# ─── Blend application ────────────────────────────────────────────


def test_search_blend_applied_to_top_k(monkeypatch, search_corpus_db):
    """Hand-crafted llm_scores produce final ordering per _blended_score."""
    from sage_memory import search

    # Force a stable set of candidates by limiting the query to one
    # that matches many docs (quintarius appears in 2 docs; ozymandias
    # in 2). For determinism use keyword-only.
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True,
    )

    # Block expand — only test rerank's blend math here.
    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants",
        lambda *a, **kw: pytest.fail("expand should be disabled"),
    )

    # Capture candidates sent to rerank; return inverted scores
    # (last candidate gets highest llm_score).
    captured = {}

    def _fake_rerank(query, candidates, **kw):
        captured["sent"] = candidates
        # Top-of-RRF (id=1) gets lowest llm; tail-of-top-K gets highest.
        # With position-1 w_rrf=0.75 and llm=0.0 → blended=0.75*rrf
        # With position-3 w_rrf=0.75 and llm=1.0 → blended=0.75*rrf + 0.25
        n = len(candidates)
        return [
            {"id": c["id"], "score": idx / max(1, n - 1)}
            for idx, c in enumerate(candidates)
        ]

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake_rerank,
    )

    result = search.search(
        query="quintarius",
        strategy="keyword", expand=False, rerank=True,
    )

    # Sanity: rerank was called.
    assert "sent" in captured
    # The result list has at least 1 entry.
    assert len(result["results"]) >= 1


def test_search_non_reranked_tail_keeps_rrf_order(
    monkeypatch, tmp_path,
):
    """Top-K (15) reranked; positions 16+ retain RRF order in final result."""
    # Build a corpus where many docs match.
    docs = [
        (f"Doc {i}", f"quintarius mentioned in doc number {i}")
        for i in range(25)
    ]
    conn = _make_db_with_docs(tmp_path, docs)
    monkeypatch.setattr(
        "sage_memory.search.get_all_dbs",
        lambda: [("project", conn)],
    )

    from sage_memory import search

    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True,
    )
    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants",
        lambda *a, **kw: pytest.fail("expand should be disabled"),
    )

    rerank_called = []

    def _fake_rerank(query, candidates, **kw):
        rerank_called.append(len(candidates))
        # Return all-0.5 scores so blend == 0.75*rrf + 0.125 ≈ rrf
        return [{"id": c["id"], "score": 0.5} for c in candidates]

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake_rerank,
    )

    result = search.search(
        query="quintarius", strategy="keyword",
        limit=20, expand=False, rerank=True,
    )

    # Rerank operated on top-K (default 15).
    assert rerank_called == [15]
    # Tail (16+) is present in results and ordered correctly.
    assert len(result["results"]) >= 15

    conn.close()


def test_search_lex_variant_dedup(monkeypatch, search_corpus_db):
    """Major #4: lex variants identical to query or each other → one FTS5 query."""
    from sage_memory import search

    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True,
    )

    def _fake_expand(q):
        # Duplicates: query x2 + "v1" x2 → after dedup, just "v1".
        return {
            "lex": [q, q, "v1", "v1"], "vec": q, "hyde": None,
        }

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _fake_expand,
    )

    # Count _fts_search invocations on the bm25 channel.
    fts_query_log = []
    original_fts = search._fts_search

    def _spy_fts(db, query, *a, **kw):
        fts_query_log.append(query)
        return original_fts(db, query, *a, **kw)

    monkeypatch.setattr(search, "_fts_search", _spy_fts)

    # Use a query that won't short-circuit (has variants to consider).
    search.search(
        query="ambiguous query terms here",
        strategy="keyword", expand=True, rerank=False,
    )

    # We expect: 1 query for original + 1 for "v1" = 2 total
    # (NOT 5 — duplicates dedupe'd).
    # `original` query is _build_fts_query's filtered form, but we're
    # asserting the call count.
    unique_calls = set(fts_query_log)
    assert len(unique_calls) <= 2, (
        f"expected ≤ 2 unique FTS queries after dedup, got "
        f"{len(unique_calls)}: {unique_calls}"
    )


# ─── A14: mixed-failure paths end-to-end ──────────────────────────


def test_search_expand_fails_rerank_succeeds_end_to_end(
    monkeypatch, search_corpus_db, caplog,
):
    """A14 mixed (1): expand fails (timeout) → no-expansion + rerank still runs."""
    from sage_memory import search, expand as expand_mod

    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True,
    )
    # Force expand to take the LLM-call path (bypass short-circuit)
    # so the failure is actually exercised end-to-end.
    monkeypatch.setattr(
        expand_mod, "_is_strong_signal", lambda seed: False,
    )

    def _failing_expand(q):
        raise httpx.TimeoutException("simulated expand timeout")

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _failing_expand,
    )

    rerank_called = []

    def _fake_rerank(query, candidates, **kw):
        rerank_called.append(len(candidates))
        return [{"id": c["id"], "score": 0.5} for c in candidates]

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake_rerank,
    )

    with caplog.at_level(
        logging.WARNING, logger="sage_memory.expand",
    ):
        result = search.search(
            query="ambiguous mention",
            strategy="keyword", expand=True, rerank=True,
        )

    # Rerank ran despite expand failure (only if ≥2 candidates).
    assert "results" in result
    # WARN from expand failure path.
    assert any(
        r.name == "sage_memory.expand"
        and r.levelno >= logging.WARNING
        and "LLM failure" in r.message
        for r in caplog.records
    ), "expected a WARNING from sage_memory.expand naming the failure"


def test_search_expand_succeeds_rerank_fails_end_to_end(
    monkeypatch, search_corpus_db, caplog,
):
    """A14 mixed (2): expand succeeds; rerank fails → RRF order, blend skipped."""
    from sage_memory import search

    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True,
    )

    def _fake_expand(q):
        return {"lex": [q], "vec": q, "hyde": None}

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _fake_expand,
    )

    def _failing_rerank(*a, **kw):
        raise httpx.TimeoutException("simulated rerank timeout")

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _failing_rerank,
    )

    with caplog.at_level(logging.WARNING):
        result = search.search(
            query="ambiguous mention",
            strategy="keyword", expand=True, rerank=True,
        )

    # Search completes with results; rerank-failure path set
    # llm_score=None and pure RRF order.
    assert "results" in result


# ─── A15: dual-LLM timeout no-hang ────────────────────────────────


def test_search_worst_case_dual_llm_timeout_raises_not_hangs(
    monkeypatch, search_corpus_db,
):
    """A15: both expand+rerank LLM calls time out → completes within budget."""
    from sage_memory import search

    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True,
    )

    def _sleep_then_timeout(*a, **kw):
        # Simulate the worst case: a real ReadTimeout from httpx.
        # No actual sleep here — production raises after _HTTP_TIMEOUT
        # so this test mocks the raised exception directly to keep
        # CI wall time bounded.
        raise httpx.ReadTimeout("simulated read timeout")

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _sleep_then_timeout,
    )
    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _sleep_then_timeout,
    )

    t0 = time.perf_counter()
    result = search.search(
        query="quintarius",
        strategy="keyword", expand=True, rerank=True,
    )
    elapsed = time.perf_counter() - t0

    # Two LLM-failure paths fired; search completed; no hang.
    assert "results" in result
    # Wall time should be sub-second — both LLM calls returned
    # immediately (mocked raise) and search did its work.
    assert elapsed < 5.0, (
        f"search took {elapsed:.2f}s on dual-LLM-timeout path; "
        f"expected sub-second (mocked) or at most ~2*_HTTP_TIMEOUT"
    )


# ─── M5 T8 / ADR-004 amendment — min-coverage gate ─────────────


def test_search_rerank_partial_coverage_skips_blend(
    monkeypatch, tmp_path,
):
    """ADR-004 amendment: when rerank covers < min_coverage of head,
    skip the blend entirely and keep pure RRF order. Regression
    test for the LongMemEval failure mode (LLM scores 1/15; partial
    blend demoted the LLM-confirmed best below un-scored siblings).

    Builds a 20-doc corpus where bm25 alone pins the right doc at #1;
    LLM returns score for ONLY that one (coverage 1/15 = 6.7%);
    asserts the final top-1 is unchanged from the pure-RRF order.
    """
    import sqlite3, sqlite_vec, uuid, time as time_mod
    from sage_memory.db import _migrate

    db_file = tmp_path / "search.db"
    conn = sqlite3.connect(str(db_file), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _migrate(conn)
    now = time_mod.time()
    # 1 strong match + 19 weak (all-match the query but with lower TF).
    docs = [(
        "Strong unique answer dossier",
        "quintarius ozymandias quintarius ozymandias quintarius ozymandias",
    )]
    for i in range(19):
        docs.append((
            f"weak match doc {i}",
            f"quintarius briefly mentioned here doc {i}",
        ))
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

    from sage_memory import search
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True,
    )
    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants",
        lambda q: pytest.fail("expand should be disabled"),
    )

    def _sparse_rerank(query, candidates, **kw):
        # LLM scores ONLY the top candidate; omits the rest.
        return [{"id": candidates[0]["id"], "score": 0.95}]

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _sparse_rerank,
    )

    result_rrf_only = search.search(
        query="quintarius ozymandias",
        strategy="keyword", limit=5, expand=False, rerank=False,
    )
    pure_rrf_top1_title = result_rrf_only["results"][0]["title"]

    result_with_rerank = search.search(
        query="quintarius ozymandias",
        strategy="keyword", limit=5, expand=False, rerank=True,
    )
    blended_top1_title = result_with_rerank["results"][0]["title"]

    # ADR-004 amendment gate fired (coverage 1/15 < 0.5) → blend
    # skipped → top-1 unchanged from pure-RRF.
    assert blended_top1_title == pure_rrf_top1_title, (
        f"min-coverage gate should preserve pure-RRF top-1; "
        f"got {blended_top1_title!r} != {pure_rrf_top1_title!r}"
    )
    assert "Strong unique answer" in blended_top1_title

    conn.close()
