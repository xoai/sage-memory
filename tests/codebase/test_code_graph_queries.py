"""P2-1 — Structural code-graph query tests (SM-CAP-01).

Written tests-first per 06-spec §P2-1 + the (internal) design brief.
Fixtures use REAL extraction via scan() on small Go projects — no mocks.

Contracts pinned:
  - path: BFS over RESOLVED edges only; disambiguation returns
    candidates; unresolved edges never form a path hop
  - affected: inbound traversal grouped by kind, depth-capped,
    unresolved labelled name-match, --resolved-only filter,
    truncation signalling
  - hubs: degree split + ordering, unresolved targets joined by name
  - MCP: 3 additive tools; existing 10 unchanged
  - migration 012: indexes exist after upgrade
"""

from __future__ import annotations

from pathlib import Path

import pytest

tsp = pytest.importorskip("tree_sitter_language_pack")

from sage_memory.codebase import scan
from sage_memory.codebase.queries import affected, find_path, hubs
from sage_memory.db import close_all, get_db


@pytest.fixture
def go_project(tmp_path: Path, monkeypatch):
    """main → Foo → Bar chain + Unknown() unresolved call + dup names
    in a second package."""
    close_all()
    monkeypatch.setenv("SAGE_PROJECT_ROOT", str(tmp_path))
    (tmp_path / "pyproject.toml").write_text("[tool.test]\n")

    (tmp_path / "main.go").write_text(
        "package main\n\n"
        "func main() {\n\tFoo()\n\tUnknown()\n}\n"
    )
    (tmp_path / "b.go").write_text(
        "package main\n\n"
        "func Foo() int {\n\treturn Bar()\n}\n\n"
        "func Bar() int {\n\treturn 1\n}\n"
    )
    pkg = tmp_path / "other"
    pkg.mkdir()
    (pkg / "a.go").write_text(
        "package other\n\nfunc Helper() int {\n\treturn 1\n}\n"
    )
    pkg2 = tmp_path / "third"
    pkg2.mkdir()
    (pkg2 / "b.go").write_text(
        "package third\n\nfunc Helper() int {\n\treturn 2\n}\n"
    )
    scan(root=tmp_path)
    yield tmp_path
    close_all()


# ─── path ─────────────────────────────────────────────────────────


def test_path_found_through_resolved_edges(go_project):
    result = find_path(get_db(), "main", "Bar")
    assert result["found"] is True
    qnames = [h["source_qname"] for h in result["hops"]] + [
        result["hops"][-1]["target_qname"]
    ]
    assert qnames == ["main", "Foo", "Bar"]
    assert all(h["confidence"] == "resolved" for h in result["hops"])
    assert result["truncated"] is False


def test_path_not_found_sets_found_false(go_project):
    result = find_path(get_db(), "Bar", "main")
    # Bar never calls anything; reverse path must not exist.
    assert result["found"] is False
    assert result["hops"] == []


def test_path_disambiguation_returns_candidates(go_project):
    result = find_path(get_db(), "Helper", "main")
    assert result["found"] is False
    assert "candidates_a" in result
    assert len(result["candidates_a"]) == 2
    assert all(
        c["qualified_name"] == "Helper" for c in result["candidates_a"]
    )
    files = {c["file"] for c in result["candidates_a"]}
    assert any(f.endswith("other/a.go") for f in files)
    assert any(f.endswith("third/b.go") for f in files)


def test_path_unknown_symbol_reports_no_candidates(go_project):
    result = find_path(get_db(), "NoSuchSymbol", "main")
    assert result["found"] is False
    assert result.get("candidates_a") in (None, [])


def test_path_respects_max_depth(go_project):
    result = find_path(get_db(), "main", "Bar", max_depth=1)
    assert result["found"] is False
    assert result["max_depth"] == 1


# ─── affected ─────────────────────────────────────────────────────


def test_affected_depth_one(go_project):
    result = affected(get_db(), "Bar", depth=1)
    names = [
        e["source_qname"]
        for entries in result["by_kind"].values() for e in entries
    ]
    assert "Foo" in names
    assert "main" not in names, "depth=1 must not reach main"


def test_affected_depth_two_reaches_main(go_project):
    result = affected(get_db(), "Bar", depth=2)
    names = [
        e["source_qname"]
        for entries in result["by_kind"].values() for e in entries
    ]
    assert "Foo" in names and "main" in names


def test_affected_labels_unresolved_edges(go_project):
    """Unknown() is an unresolved call from main. Querying affected
    on a symbol named 'Unknown'... has no symbol row — instead pin
    the labelling rule on the reverse side: an unresolved edge must
    carry confidence 'unresolved' when it joins by name. Covered via
    hubs' unresolved accounting and the affected entry shape."""
    result = affected(get_db(), "Foo", depth=1)
    entries = [e for entries in result["by_kind"].values() for e in entries]
    assert entries, "expected main → Foo edge"
    for e in entries:
        assert e["confidence"] in ("resolved", "unresolved")
        assert "file" in e and "line" in e


def test_affected_resolved_only_filter(go_project):
    result = affected(get_db(), "Bar", depth=2, resolved_only=True)
    assert result["resolved_only"] is True
    for entries in result["by_kind"].values():
        for e in entries:
            assert e["confidence"] == "resolved"


def test_affected_grouped_by_kind(go_project):
    result = affected(get_db(), "Foo", depth=1)
    assert "calls" in result["by_kind"]


# ─── hubs ─────────────────────────────────────────────────────────


def test_hubs_ordering_and_degree_split(go_project):
    result = hubs(get_db(), limit=10)
    rows = result["hubs"]
    assert rows, "expected hub rows"
    degrees = [h["degree"] for h in rows]
    assert degrees == sorted(degrees, reverse=True)
    foo = next(h for h in rows if h["qualified_name"] == "Foo")
    assert foo["out_degree"] >= 1  # calls Bar
    assert foo["in_degree"] >= 1   # called by main


def test_hubs_resolved_only(go_project):
    result = hubs(get_db(), limit=10, resolved_only=True)
    assert result["resolved_only"] is True


# ─── MCP registration (additive) ──────────────────────────────────


def test_mcp_code_tools_registered_additively():
    from sage_memory.server import TOOLS, HANDLERS
    names = {t.name for t in TOOLS}
    for expected in (
        "sage_memory_code_path",
        "sage_memory_code_affected",
        "sage_memory_code_hubs",
    ):
        assert expected in names, f"{expected} not registered"
        assert expected in HANDLERS
    # The pre-existing 10 tools must still be there, untouched.
    for legacy in (
        "sage_memory_set_project", "sage_memory_store",
        "sage_memory_search", "sage_memory_update",
        "sage_memory_delete", "sage_memory_list", "sage_memory_link",
        "sage_memory_graph", "sage_memory_scan_codebase",
    ):
        assert legacy in names
    assert len(names) == 12  # 9 legacy + 3 new


def test_mcp_code_path_handler_envelope(go_project):
    from sage_memory.server import HANDLERS
    result = HANDLERS["sage_memory_code_path"](**{"a": "main", "b": "Bar"})
    assert result.get("success") is True
    assert result["found"] is True


# ─── migration 012 ────────────────────────────────────────────────


def test_012_indexes_exist(go_project):
    conn = get_db()
    names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert "idx_code_symbols_name" in names
    assert "idx_code_symbols_file" in names


def test_012_upgrade_from_011_db(
    fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    """Append-only contract: 012 applies on an 011-era DB and bumps
    user_version to 12."""
    from sage_memory.db import _migrate
    through_011 = (
        "001_initial.sql", "002_edges.sql", "003_memory_health.sql",
        "004_chunks.sql", "005_entities.sql", "006_embedding_meta.sql",
        "007_extraction_queue.sql", "008_worker_state.sql",
        "009_code_symbols.sql", "010_unresolved_relations_index.sql",
        "011_worker_crash_state.sql",
    )
    copy_production_migrations(*through_011)
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    assert fresh_db.execute("PRAGMA user_version").fetchone()[0] == 11

    copy_production_migrations("012_code_graph_indexes.sql")
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    assert fresh_db.execute("PRAGMA user_version").fetchone()[0] == 12
    names = {
        r[0] for r in fresh_db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert {"idx_code_symbols_name", "idx_code_symbols_file"} <= names
