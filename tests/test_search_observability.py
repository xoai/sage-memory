"""P1-4 — Observable failures on the search path (SM-REL-02).

Written tests-first per 05-spec-phase1 §P1-4. The defect: silent
`except Exception: pass` (and in `_vec_search`, NO guard at all)
made a failing retrieval channel indistinguishable from an empty one.

Contracts pinned:
  - Vector channel raises → BM25 results still returned AND the
    failure is logged with channel context (never the query text)
  - Access-tracking flush failure → search result unaffected, failure
    logged at debug, failure counter incremented
  - Public MCP response shape unchanged for the default path
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from sage_memory import search as search_mod
from sage_memory.db import close_all


@pytest.fixture
def project_db(tmp_path: Path, monkeypatch):
    close_all()
    monkeypatch.setenv("SAGE_PROJECT_ROOT", str(tmp_path))
    # Hermeticity (per the stdio-test lesson): search's default scope
    # includes the GLOBAL DB — without an isolated HOME the developer's
    # real memories outrank the fixture's on any query.
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    (tmp_path / "pyproject.toml").write_text("[tool.test]\n")
    yield tmp_path
    close_all()


def _store_memory(content: str, title: str) -> None:
    from sage_memory.store import store
    store(content=content, title=title, tags=[])


def test_vector_channel_failure_returns_bm25_and_logs(
    project_db, monkeypatch, caplog,
):
    _store_memory("observability sentinel needle", "obs-sentinel")

    # The vector channel is quality-gated (LocalEmbedder 0.45 sits
    # below the threshold — default state is BM25-only). Stub a
    # high-quality embedder so the channel actually runs.
    class _StubEmbedder:
        name = "stub"
        dim = 384
        quality = 0.9
        def embed(self, _text):
            return [0.1] * 384

    monkeypatch.setattr(search_mod, "get_embedder", lambda: _StubEmbedder())

    def _boom(*args, **kwargs):
        raise RuntimeError("vec0 exploded")

    monkeypatch.setattr(search_mod, "_vec_search", _boom)
    monkeypatch.setattr(search_mod, "_vec_search_chunks", _boom)

    with caplog.at_level(logging.WARNING, logger="sage-memory"):
        result = search_mod.search(query="observability sentinel needle")

    titles = [r.get("title") for r in result["results"]]
    assert "obs-sentinel" in titles, (
        f"BM25 results must survive a vector-channel failure; got {titles}"
    )
    vec_logs = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "vector" in r.message
    ]
    assert vec_logs, (
        f"vector failure must be logged at warning; got "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )
    # Never the full query text at warning level (content leak).
    assert not any(
        "observability sentinel needle" in r.message for r in vec_logs
    ), "query text leaked into warning logs"


def test_access_flush_failure_is_debug_logged_and_counted(
    project_db, caplog, monkeypatch,
):
    """The access-tracking cluster is genuinely non-fatal — but the
    failure must be visible at debug and counted."""

    class _ExplodingDb:
        def execute(self, *a, **k):
            raise RuntimeError("db locked forever")
        def commit(self):
            raise RuntimeError("db locked forever")

    search_mod._access_buffer = [("m1", 1.0, "project")] * (
        search_mod._ACCESS_FLUSH_SIZE
    )
    before = search_mod._ACCESS_FLUSH_FAILURES
    with caplog.at_level(logging.DEBUG, logger="sage-memory"):
        # Must NOT raise.
        search_mod._flush_access([("project", _ExplodingDb())])
    assert search_mod._ACCESS_FLUSH_FAILURES > before, (
        "access-flush failures must be counted"
    )
    assert any(
        "access" in r.message.lower() for r in caplog.records
    ), f"access failure not logged: {[r.message for r in caplog.records]}"
    search_mod._access_buffer = []


def test_public_response_shape_unchanged(project_db):
    _store_memory("shape check memory", "shape-check")
    result = search_mod.search(query="shape check")
    # Baseline envelope per search.py:438 ({results,total,query,timings})
    # plus M3b/M4 additive keys — pin the exact key set so any public
    # shape change is a deliberate, reviewed event.
    assert set(result.keys()) == {
        "results", "total", "query", "timings",
    }, f"public envelope drifted: {sorted(result.keys())}"
