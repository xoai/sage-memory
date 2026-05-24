"""M2.3 — hub.search.fan_out_search.

5 tests per plan.md M2.3 done-when:
  1. empty hub → searches global only (or returns empty if global empty)
  2. 3 projects each with distinct seeded content + global → fan-out
     finds memories from all 4 sources
  3. results sorted by RRF score (descending)
  4. ``source`` field set to hub project name per result
  5. RRF helper module-source identity — single source of truth in
     ``search.rrf_fuse`` (auto-review MINOR-substantive #6)
"""

from __future__ import annotations

import inspect
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from sage_memory import search as _search_mod
from sage_memory.db import (
    _open, close_all, get_project_db_path, override_project_root,
)
from sage_memory.embedder import (
    EMBEDDING_DIM, LocalEmbedder, set_embedder,
)
from sage_memory.hub import config as hub_config
from sage_memory.hub import search as hub_search


class _SeedEmbedder:
    """Deterministic, fast embedder for fixture seeding."""
    name = "test-seed"
    version = "v1"
    dim = EMBEDDING_DIM
    quality = 0.9
    max_input_chars = 8192

    def embed(self, text):
        # Distinct vectors for each input so search ranking is stable
        # but we don't depend on FTS5 implementation details.
        h = abs(hash(text)) % 100
        return [(h + i) / 100.0 for i in range(EMBEDDING_DIM)]


# ─── Fixture: hub config + N seeded project DBs ───────────────────


@pytest.fixture
def seeded_projects(tmp_path, monkeypatch):
    """Create 3 project DBs, each with one distinct memory + a shared
    "shipping" memory so fan-out has unique-per-project AND overlapping
    content to rank. Yields (hub_config_path, list_of_project_roots)."""
    # Isolate from any real ~/.sage-hub.yaml / ~/.sage-memory.
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))

    set_embedder(_SeedEmbedder())

    project_roots: list[Path] = []
    for label, unique_content in [
        ("backend", "Backend service handles authentication shipping"),
        ("frontend", "Frontend UI renders the checkout shipping page"),
        ("ops", "Ops runbook covers deployment shipping pipeline"),
    ]:
        root = tmp_path / label
        root.mkdir()
        (root / ".git").mkdir()

        close_all()
        override_project_root(root)
        db_path = get_project_db_path(root)
        conn = _open(db_path)
        # Seed via the production store path so FTS5 indexing fires.
        from sage_memory.store import store
        store(content=unique_content, title=f"{label} note", tags=[label])
        # A shared term ('shipping') in each project + one project-
        # specific term gives the RRF fan-out something to rank.
        conn.commit()
        project_roots.append(root)

    # Hub config registers all 3 projects.
    hub_path = tmp_path / ".sage-hub.yaml"
    cfg = hub_config.init(hub_path)
    for root in project_roots:
        cfg = hub_config.add_project(cfg, root.name, root)
    hub_config.save(cfg, hub_path)

    yield hub_path, project_roots

    close_all()
    set_embedder(LocalEmbedder())


# ─── 1. Empty hub → global only ───────────────────────────────────


def test_empty_hub_searches_zero_sources_cleanly(tmp_path, monkeypatch):
    """An empty hub config (or no hub config at all) — fan_out_search
    must not crash and returns an empty envelope. Pinning the no-data
    case so the M2.5 MCP integration doesn't have to special-case it."""
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))

    hub_path = tmp_path / ".sage-hub.yaml"
    hub_config.init(hub_path)

    result = hub_search.fan_out_search(
        "anything", hub_path=hub_path, limit=5,
    )
    assert result["results"] == []
    assert result["total"] == 0
    assert result["query"] == "anything"


# ─── 2. Fan-out finds memories from all registered projects ───────


def test_fan_out_returns_memories_from_each_project(seeded_projects):
    hub_path, _roots = seeded_projects
    result = hub_search.fan_out_search(
        "shipping", hub_path=hub_path, limit=10,
    )
    sources_in_results = {r["source"] for r in result["results"]}
    # Each of the 3 registered projects seeded a 'shipping' memory.
    assert {"backend", "frontend", "ops"}.issubset(sources_in_results), (
        f"fan-out missed a source; results={result['results']!r}"
    )


# ─── 3. Results sorted by RRF score (descending) ──────────────────


def test_results_are_sorted_by_rrf_score_descending(seeded_projects):
    hub_path, _roots = seeded_projects
    result = hub_search.fan_out_search(
        "shipping", hub_path=hub_path, limit=10,
    )
    scores = [r["rrf_score"] for r in result["results"]]
    assert scores, "fan-out must return at least one result"
    assert scores == sorted(scores, reverse=True), (
        f"results not sorted by RRF score descending: {scores!r}"
    )


# ─── 4. `source` field set per result ─────────────────────────────


def test_source_field_set_to_hub_project_name(seeded_projects):
    hub_path, _roots = seeded_projects
    result = hub_search.fan_out_search(
        "shipping", hub_path=hub_path, limit=10,
    )
    valid_sources = {"backend", "frontend", "ops", "global"}
    for r in result["results"]:
        assert "source" in r, (
            f"result missing 'source' field: {r!r}"
        )
        assert r["source"] in valid_sources, (
            f"unexpected source {r['source']!r}; valid={valid_sources!r}"
        )


# ─── 5. RRF helper module-source identity ─────────────────────────


def test_fan_out_includes_global_db_when_present(tmp_path, monkeypatch):
    """Regression for /review M2 CRITICAL #1: hub.search must reach the
    canonical global DB via ``db.get_global_db_path()``, not a
    hardcoded filename. Previously hardcoded ``sage.db``; canonical
    name is ``memory.db`` per db.DB_NAME."""
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    set_embedder(_SeedEmbedder())

    from sage_memory.db import get_global_db_path
    from sage_memory.store import store

    # Seed the canonical global DB so it participates in fan-out.
    global_db = get_global_db_path()
    global_db.parent.mkdir(parents=True, exist_ok=True)
    close_all()
    _open(global_db)
    store(
        content="Global note about shipping pipeline",
        title="global shipping",
        tags=["global-only"],
        scope="global",
    )

    # Empty hub config — fan-out must STILL include global.
    hub_path = tmp_path / ".sage-hub.yaml"
    hub_config.init(hub_path)

    result = hub_search.fan_out_search(
        "shipping", hub_path=hub_path, limit=10,
    )
    sources_used = result.get("sources") or []
    sources_in_results = {r["source"] for r in result["results"]}

    assert "global" in sources_used, (
        f"global must appear in sources list; got {sources_used!r}"
    )
    assert "global" in sources_in_results, (
        f"global must contribute at least one result; got "
        f"{result['results']!r}"
    )

    close_all()
    set_embedder(LocalEmbedder())


def test_fan_out_rejects_empty_hub_projects_list():
    """Regression for /review M2 MAJOR #4: hub_projects=[] is a
    malformed call (distinct from None). Reject explicitly so callers
    can't get a silently empty envelope."""
    from sage_memory.hub import search as hub_search

    with pytest.raises(ValueError) as exc:
        hub_search.fan_out_search("anything", hub_projects=[])
    assert "non-empty" in str(exc.value).lower()


def test_rrf_helper_is_single_source_of_truth():
    """ADR-008 + auto-review MINOR-substantive #6: the RRF algorithm
    must NOT be copied between search.py and hub/search.py. The
    canonical definition lives in search.py; hub/search.py imports it.

    Verified two ways:
      (a) ``is`` identity — same function object, not a copy
      (b) ``inspect.getsourcefile`` returns search.py for both names
    """
    assert hub_search.rrf_fuse is _search_mod.rrf_fuse, (
        "hub.search.rrf_fuse must be the same function object as "
        "search.rrf_fuse — not a copy"
    )
    assert (
        inspect.getsourcefile(hub_search.rrf_fuse)
        == inspect.getsourcefile(_search_mod.rrf_fuse)
    ), (
        "hub.search.rrf_fuse and search.rrf_fuse must resolve to the "
        "same source file"
    )
