"""M2.4 — `sage-memory hub search` CLI subcommand.

2 tests per plan.md M2.4 done-when:
  1. CLI happy path — search "<query>" returns matching memories
  2. --tags filter narrows results

Reuses the seeded_projects fixture pattern from test_hub_search.py
so CLI tests exercise the same fan-out path the M2.3 module tests pin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage_memory.db import (
    _open, close_all, get_project_db_path, override_project_root,
)
from sage_memory.embedder import (
    EMBEDDING_DIM, LocalEmbedder, set_embedder,
)
from sage_memory.hub import config as hub_config


class _SeedEmbedder:
    name = "test-seed"
    version = "v1"
    dim = EMBEDDING_DIM
    quality = 0.9
    max_input_chars = 8192

    def embed(self, text):
        h = abs(hash(text)) % 100
        return [(h + i) / 100.0 for i in range(EMBEDDING_DIM)]


@pytest.fixture
def hub_with_tagged_memories(tmp_path, monkeypatch):
    """Two projects with overlapping content but distinct tags so the
    --tags filter has something to slice on."""
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    set_embedder(_SeedEmbedder())

    from sage_memory.store import store

    project_roots: list[Path] = []
    for label, content, tags in [
        ("alpha", "Shared shipping pipeline note", ["m24", "alpha-only"]),
        ("beta", "Another shipping pipeline note", ["m24", "beta-only"]),
    ]:
        root = tmp_path / label
        root.mkdir()
        (root / ".git").mkdir()
        close_all()
        override_project_root(root)
        _open(get_project_db_path(root))
        store(content=content, title=f"{label} note", tags=tags)
        project_roots.append(root)

    hub_path = tmp_path / ".sage-hub.yaml"
    cfg = hub_config.init(hub_path)
    for root in project_roots:
        cfg = hub_config.add_project(cfg, root.name, root)
    hub_config.save(cfg, hub_path)

    yield hub_path

    close_all()
    set_embedder(LocalEmbedder())


def test_hub_search_cli_prints_matching_results(
    hub_with_tagged_memories, capsys,
):
    from sage_memory.cli_hub import run_hub

    rc = run_hub([
        "search", "shipping",
        "--config-path", str(hub_with_tagged_memories),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "result(s)" in out
    # Both projects' notes carry 'shipping' — both sources visible.
    assert "alpha" in out
    assert "beta" in out


def test_hub_search_cli_tags_flag_before_query_does_not_eat_query(
    hub_with_tagged_memories, capsys,
):
    """Regression for /review M2 MAJOR #2: previously the --tags flag
    greedily consumed args until the next --flag, so
    ``--tags alpha-only shipping`` would eat the query positional.
    Post-fix: --tags is one-value-per-flag (repeat for multiple tags),
    so the query positional always resolves correctly regardless of
    flag order."""
    from sage_memory.cli_hub import run_hub

    rc = run_hub([
        "search",
        "--tags", "alpha-only",
        "--config-path", str(hub_with_tagged_memories),
        "shipping",  # query AFTER --tags (the failure-mode position)
    ])
    out = capsys.readouterr().out
    assert rc == 0, f"--tags-before-query must work; rc={rc}; out={out!r}"
    assert "alpha" in out


def test_hub_search_cli_tags_filter_narrows_results(
    hub_with_tagged_memories, capsys,
):
    """--tags alpha-only restricts the fan-out to memories tagged with
    'alpha-only', dropping beta's match even though 'shipping' would
    otherwise rank it."""
    from sage_memory.cli_hub import run_hub

    rc = run_hub([
        "search", "shipping",
        "--tags", "alpha-only",
        "--config-path", str(hub_with_tagged_memories),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    # alpha matches; beta does NOT (no alpha-only tag).
    assert "alpha" in out
    assert "[beta]" not in out, (
        f"--tags filter should exclude beta; got:\n{out}"
    )
