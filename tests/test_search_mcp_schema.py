"""T3 — MCP schema additions: channels/strategy/expand/rerank.

Covers A8 (4 schema cases) and A11 (per-call > config cascade, scoped
to `channels` only — `expand`/`rerank` cascade is M4).
"""

from __future__ import annotations

import logging
import time
import uuid

import pytest

from sage_memory.db import (
    _open, get_project_db_path, close_all, override_project_root,
)
from sage_memory.embedder import (
    LocalEmbedder, set_embedder, EMBEDDING_DIM,
)
from sage_memory.search import search
from sage_memory.store import store
import sage_memory.db as _db_mod
import sage_memory.search as _search_mod


class _HighQualityTestEmbedder:
    name = "test-hq"; version = "v1"; dim = EMBEDDING_DIM
    quality = 0.9; max_input_chars = 8192
    def embed(self, text):
        h = abs(hash(text)) % 1000
        return [(h % 7) / 7.0] * EMBEDDING_DIM


@pytest.fixture
def project_db_hq(tmp_path, monkeypatch):
    for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]:
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    set_embedder(_HighQualityTestEmbedder())
    db = _open(get_project_db_path(tmp_path))

    tmp_global = tmp_path / "global_test.db"
    tmp_global_conn = _open(tmp_global)

    def fake_get_all_dbs():
        return [("project", db), ("global", tmp_global_conn)]

    monkeypatch.setattr(_db_mod, "get_all_dbs", fake_get_all_dbs)
    monkeypatch.setattr(_search_mod, "get_all_dbs", fake_get_all_dbs)

    yield db
    close_all()
    set_embedder(LocalEmbedder())


def _populate_graph(db):
    """Insert one memory + entity + mention so graph channel returns
    a non-empty result for query 'Alpha'."""
    now = time.time()
    db.execute(
        "INSERT INTO memories(id, title, content, content_hash, "
        "embedded, created_at, updated_at, accessed_at) "
        "VALUES ('M_a', 'memA', 'content about Alpha here', 'h_a', "
        "0, 1, 1, 1)"
    )
    db.execute(
        "INSERT INTO entities (id, name, name_normalized, type, "
        "mention_count, created_at, updated_at) "
        "VALUES ('E_a', 'Alpha', 'alpha', 'CONCEPT', 1, ?, ?)",
        (now, now),
    )
    db.execute(
        "INSERT INTO mentions (memory_id, entity_id, surface_form, "
        "confidence, created_at) "
        "VALUES ('M_a', 'E_a', 'Alpha', 1.0, ?)",
        (now,),
    )
    db.commit()


# ─── A8a — channels=None runs all available ──────────────────────


def test_mcp_search_channels_none_runs_all(project_db_hq):
    """channels=None (default) runs FTS + vec + graph (all available)."""
    _populate_graph(project_db_hq)
    out = search(query="Alpha", scope="project", limit=10, channels=None)
    titles = [r["title"] for r in out["results"]]
    assert "memA" in titles


# ─── A8b — channels=["graph"] returns graph-only ─────────────────


def test_mcp_search_channels_graph_only(project_db_hq):
    """channels=["graph"]: only graph channel runs. Empty graph
    returns 0 results."""
    out = search(
        query="Alpha", scope="project", limit=10, channels=["graph"],
    )
    # Entities empty → graph channel returns []
    assert out["results"] == []


def test_mcp_search_channels_graph_only_with_populated_graph(
    project_db_hq,
):
    """channels=["graph"] with populated graph returns the seed memory."""
    _populate_graph(project_db_hq)
    out = search(
        query="Alpha", scope="project", limit=10, channels=["graph"],
    )
    titles = [r["title"] for r in out["results"]]
    assert "memA" in titles


# ─── channels=[] (empty list) ─────────────────────────────────────


def test_mcp_search_channels_empty_list_returns_empty(project_db_hq):
    """channels=[] → no channels run; returns empty results with
    reason='no_channels_selected'."""
    _populate_graph(project_db_hq)
    out = search(
        query="Alpha", scope="project", limit=10, channels=[],
    )
    assert out["results"] == []
    assert out.get("reason") == "no_channels_selected"


# ─── A9 expand/rerank: M4 force-True + no-key → WARN + skip ───────


def test_mcp_search_expand_true_no_key_warns_and_skips(
    project_db_hq, caplog,
):
    """M4 (T4): expand=True with no LLM key → WARN + skip; behavior
    matches the no-expand baseline (M3b parity on free path).

    Was M3b's `..._param_accepted_logged_no_op`. M4 actually wires
    expand into search, so the contract changed: without an LLM key,
    `expand=True` raises a WARN and disables itself.
    """
    _populate_graph(project_db_hq)
    with caplog.at_level(logging.WARNING, logger="sage-memory"):
        out_default = search(query="Alpha", scope="project", limit=10)
        out_with_expand = search(
            query="Alpha", scope="project", limit=10, expand=True,
        )

    # Behavior: same results (no key → WARN+skip; M3b parity preserved)
    titles_default = [r["id"] for r in out_default["results"]]
    titles_expand = [r["id"] for r in out_with_expand["results"]]
    assert titles_default == titles_expand

    # WARN names the skipped expansion.
    assert any(
        "expand=True" in rec.message and rec.levelno >= logging.WARNING
        for rec in caplog.records
    )


def test_mcp_search_rerank_true_no_key_warns_and_skips(
    project_db_hq, caplog,
):
    """M4 (T4): rerank=True + no LLM key → WARN + skip; M3b parity."""
    _populate_graph(project_db_hq)
    with caplog.at_level(logging.WARNING, logger="sage-memory"):
        out_default = search(query="Alpha", scope="project", limit=10)
        out_with_rerank = search(
            query="Alpha", scope="project", limit=10, rerank=True,
        )

    titles_default = [r["id"] for r in out_default["results"]]
    titles_rerank = [r["id"] for r in out_with_rerank["results"]]
    assert titles_default == titles_rerank
    assert any(
        "rerank=True" in rec.message and rec.levelno >= logging.WARNING
        for rec in caplog.records
    )


# ─── A11 — per-call cascade (channels only; expand/rerank deferred) ─


def test_mcp_search_channels_per_call_overrides_default(project_db_hq):
    """Per-call channels=["graph"] overrides default; subsequent
    call without channels still uses default (no write-back)."""
    _populate_graph(project_db_hq)

    # Per-call override: graph-only
    out_graph = search(
        query="Alpha", scope="project", limit=10, channels=["graph"],
    )
    titles_graph_only = [r["id"] for r in out_graph["results"]]
    assert "M_a" in titles_graph_only

    # Subsequent call without channels: default behavior (all
    # channels). M_a is still in the results.
    out_default = search(query="Alpha", scope="project", limit=10)
    titles_default = [r["id"] for r in out_default["results"]]
    assert "M_a" in titles_default
    # Default may produce more results (FTS hits too) — they're a
    # superset of graph-only.
    assert set(titles_graph_only).issubset(set(titles_default))


# ─── Backward compat — existing M2 callers see no behavior change ─


def test_mcp_search_old_caller_no_new_params_unchanged(project_db_hq):
    """Calling search() with only the M2 signature (no channels/
    expand/rerank) returns the same shape as before. Smoke test for
    backward compatibility."""
    store(content="apple banana cherry test content")
    out = search(query="apple", scope="project", limit=5)
    # M2 result shape preserved
    assert "results" in out
    assert "total" in out
    assert "query" in out
    assert isinstance(out["results"], list)
