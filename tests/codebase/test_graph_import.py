"""P2-4 — Cross-tool code-graph import tests (SM-CAP-01 adjacent).

Written tests-first per 06-spec §P2-4 + the internal design brief
(.sage/docs/design/graph-import.md).

Contracts pinned:
  - Import maps artifact nodes/edges → code_symbols/code_relations
  - Confidence mapping: only explicit fact labels become resolved;
    everything else → unresolved (NEVER silently upgraded)
  - Provenance: imported rows carry source='import:<tool>'; native
    rows keep DEFAULT 'native'
  - Idempotent per-source replace: re-import replaces that source's
    rows only; native + other tools' rows untouched
  - Malformed entries counted, not fatal
  - Works from a file artifact only — no external package import
  - Migration 013: source column with 'native' default
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

tsp = pytest.importorskip("tree_sitter_language_pack")

from sage_memory.codebase import scan
from sage_memory.codebase.importer import import_graph
from sage_memory.db import close_all, get_db


_ARTIFACT = {
    "tool": "codemap",
    "version": "1.0.0",
    "nodes": [
        {"id": "n1", "name": "Parser", "qualified_name": "kt.Parser",
         "kind": "CLASS", "language": "kotlin",
         "file": "src/kt/Parser.kt", "line_start": 5, "line_end": 60},
        {"id": "n2", "name": "parse", "qualified_name": "kt.Parser.parse",
         "kind": "METHOD", "language": "kotlin",
         "file": "src/kt/Parser.kt", "line_start": 12, "line_end": 30},
        {"id": "n3", "name": "lex", "qualified_name": "kt.Lexer.lex",
         "kind": "METHOD", "language": "kotlin",
         "file": "src/kt/Lexer.kt", "line_start": 8, "line_end": 20},
    ],
    "edges": [
        # explicit fact label → resolved
        {"source": "n2", "target": "n3", "kind": "calls",
         "confidence": "verified", "line": 14},
        # inferred → unresolved (must NOT be upgraded)
        {"source": "n1", "target": "n2", "kind": "calls",
         "confidence": "inferred", "line": 7},
        # missing confidence → unresolved
        {"source": "n2", "target": "n1", "kind": "imports", "line": 2},
        # malformed (no target) → counted + skipped
        {"source": "n2", "kind": "calls"},
    ],
}


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    close_all()
    monkeypatch.setenv("SAGE_PROJECT_ROOT", str(tmp_path))
    (tmp_path / "pyproject.toml").write_text("[tool.test]\n")
    yield tmp_path
    close_all()


@pytest.fixture
def artifact_file(tmp_path: Path) -> Path:
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(_ARTIFACT))
    return p


def _relations(conn):
    return [
        dict(r) for r in conn.execute(
            "SELECT source_symbol_id, target_symbol_id, target_name, "
            "kind, confidence, source FROM code_relations"
        )
    ]


def test_import_maps_nodes_and_edges(project, artifact_file):
    result = import_graph(get_db(), artifact_file)
    assert result["imported"] == 3
    assert result["skipped_malformed"] == 1
    assert result["edges_resolved"] == 1
    assert result["edges_unresolved"] == 2

    conn = get_db()
    rels = _relations(conn)
    assert all(r["source"] == "import:codemap" for r in rels)
    verified = [r for r in rels if r["confidence"] == "resolved"]
    assert len(verified) == 1
    # The verified edge's endpoints must both be mapped to real rows.
    assert verified[0]["target_symbol_id"] is not None


def test_confidence_never_silently_upgraded(project, artifact_file):
    import_graph(get_db(), artifact_file)
    rels = _relations(get_db())
    inferred = [r for r in rels if r["kind"] == "calls"
                and r["confidence"] != "resolved"]
    assert inferred, "expected the 'inferred' edge to remain unresolved"
    # 'inferred' and missing-confidence edges are unresolved.
    assert all(r["confidence"] == "unresolved" for r in inferred)


def test_reimport_replaces_only_own_source(project, artifact_file):
    import_graph(get_db(), artifact_file)
    first = _relations(get_db())
    assert len(first) == 3

    # Import again — same artifact, same tool: replace, not duplicate.
    import_graph(get_db(), artifact_file)
    second = _relations(get_db())
    assert len(second) == 3, (
        f"re-import duplicated rows: {len(first)} → {len(second)}"
    )


def test_native_rows_untouched_by_import(project, artifact_file):
    # Native scan first (Go file sage-memory parses itself).
    (project / "main.go").write_text(
        "package main\n\nfunc main() {\n\tFoo()\n}\n\nfunc Foo() {}\n"
    )
    scan(root=project)
    native_before = _relations(get_db())
    assert native_before
    assert all(r["source"] == "native" for r in native_before)

    import_graph(get_db(), artifact_file)
    after = _relations(get_db())
    native_after = [r for r in after if r["source"] == "native"]
    imported = [r for r in after if r["source"] == "import:codemap"]
    assert len(native_after) == len(native_before), (
        "import clobbered native rows"
    )
    assert len(imported) == 3


def test_tool_override(project, artifact_file):
    result = import_graph(get_db(), artifact_file, tool="custom")
    assert result["tool"] == "custom"
    rels = _relations(get_db())
    assert all(r["source"] == "import:custom" for r in rels)


def test_two_sources_coexist(project, artifact_file, tmp_path):
    import_graph(get_db(), artifact_file)
    second = tmp_path / "graph2.json"
    payload = dict(_ARTIFACT)
    payload["tool"] = "other"
    payload["edges"] = _ARTIFACT["edges"][:1]  # 1 valid edge
    second.write_text(json.dumps(payload))
    import_graph(get_db(), second)

    rels = _relations(get_db())
    codemap = [r for r in rels if r["source"] == "import:codemap"]
    other = [r for r in rels if r["source"] == "import:other"]
    assert len(codemap) == 3 and len(other) == 1


def test_p2_1_queries_see_imported_edges(project, artifact_file):
    import_graph(get_db(), artifact_file)
    from sage_memory.codebase.queries import affected
    result = affected(get_db(), "kt.Lexer.lex", depth=1)
    entries = [
        e for entries in result["by_kind"].values() for e in entries
    ]
    assert any(
        e["source_qname"] == "kt.Parser.parse"
        and e["confidence"] == "resolved"
        for e in entries
    ), f"imported edge not visible to affected(); got {entries!r}"


def test_013_source_column_default_native(project):
    conn = get_db()
    cols = {
        r[1]: r[4] for r in conn.execute(
            "PRAGMA table_info(code_relations)"
        )
    }
    assert "source" in cols
    assert cols["source"] == "'native'"


def test_013_upgrade_from_012_db(
    fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    from sage_memory.db import _migrate
    through_012 = (
        "001_initial.sql", "002_edges.sql", "003_memory_health.sql",
        "004_chunks.sql", "005_entities.sql", "006_embedding_meta.sql",
        "007_extraction_queue.sql", "008_worker_state.sql",
        "009_code_symbols.sql", "010_unresolved_relations_index.sql",
        "011_worker_crash_state.sql", "012_code_graph_indexes.sql",
    )
    copy_production_migrations(*through_012)
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    assert fresh_db.execute("PRAGMA user_version").fetchone()[0] == 12

    copy_production_migrations("013_relation_source.sql")
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    assert fresh_db.execute("PRAGMA user_version").fetchone()[0] == 13
    cols = {
        r[1] for r in fresh_db.execute(
            "PRAGMA table_info(code_relations)"
        )
    }
    assert "source" in cols
