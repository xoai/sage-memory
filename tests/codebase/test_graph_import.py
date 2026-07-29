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


# ─── /review findings (5f60fbd) ───────────────────────────────────


def test_overloaded_nodes_same_file_qname_coexist(project, tmp_path):
    """Review #1 (high): two nodes with the same qualified_name in the
    same file (overloaded methods — idiomatic in Kotlin/Java) must
    BOTH import, and edges sourced from either must not dangle."""
    artifact = tmp_path / "overloads.json"
    artifact.write_text(json.dumps({
        "tool": "codemap",
        "nodes": [
            {"id": "n1", "name": "parse",
             "qualified_name": "kt.Parser.parse", "kind": "METHOD",
             "language": "kotlin", "file": "src/kt/P.kt",
             "line_start": 10, "line_end": 20},
            {"id": "n2", "name": "parse",
             "qualified_name": "kt.Parser.parse", "kind": "METHOD",
             "language": "kotlin", "file": "src/kt/P.kt",
             "line_start": 25, "line_end": 40},
            {"id": "n3", "name": "lex",
             "qualified_name": "kt.Lexer.lex", "kind": "METHOD",
             "language": "kotlin", "file": "src/kt/L.kt",
             "line_start": 1, "line_end": 5},
        ],
        "edges": [
            {"source": "n1", "target": "n3", "kind": "calls",
             "confidence": "verified", "line": 12},
            {"source": "n2", "target": "n3", "kind": "calls",
             "confidence": "verified", "line": 27},
        ],
    }))
    result = import_graph(get_db(), artifact)
    # Overloads differ by line_start → both are valid distinct
    # symbols (UNIQUE key includes line_start); n3 imports too.
    assert result["imported"] == 3
    conn = get_db()
    # Both edges must land (no FK crash, no dangling source).
    rels = [dict(r) for r in conn.execute(
        "SELECT confidence, source_symbol_id FROM code_relations"
    )]
    assert len(rels) == 2
    assert all(r["confidence"] == "resolved" for r in rels)


def test_malformed_line_numbers_counted_not_fatal(project, tmp_path):
    """Review #2 (medium): int(None) / int('abc') line values must be
    coerced or counted as malformed — never crash, never abort."""
    artifact = tmp_path / "badlines.json"
    artifact.write_text(json.dumps({
        "tool": "codemap",
        "nodes": [
            {"id": "n1", "name": "A", "qualified_name": "x.A",
             "kind": "FUNCTION", "language": "kotlin",
             "file": "src/x/A.kt", "line_start": None, "line_end": 10},
            {"id": "n2", "name": "B", "qualified_name": "x.B",
             "kind": "FUNCTION", "language": "kotlin",
             "file": "src/x/B.kt", "line_start": 5, "line_end": "abc"},
        ],
        "edges": [
            {"source": "n1", "target": "n2", "kind": "calls",
             "confidence": "verified", "line": None},
        ],
    }))
    result = import_graph(get_db(), artifact)
    # Nodes with un-coercible lines are malformed; the edge between
    # remaining nodes still imports.
    assert result["skipped_malformed"] >= 1
    assert result["imported"] >= 1


def test_failed_import_leaves_no_partial_state(project, tmp_path):
    """Review #2 (medium): an artifact-level failure mid-import must
    roll back — no partial rows persisted."""
    conn = get_db()
    artifact = tmp_path / "ok.json"
    artifact.write_text(json.dumps(_ARTIFACT))

    # sqlite3.Connection is a C type — its execute() can't be
    # monkeypatched. Fail mid-import via the file-memory helper
    # instead: second node processed → raise → rollback expected.
    from sage_memory.codebase import importer as imp_mod
    calls = {"n": 0}
    real_upsert = imp_mod._upsert_import_file_memory

    def _fail_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated mid-import failure")
        return real_upsert(*args, **kwargs)

    import unittest.mock as mock
    with mock.patch.object(
        imp_mod, "_upsert_import_file_memory", _fail_second,
    ):
        with pytest.raises(RuntimeError):
            import_graph(conn, artifact)

    rels = conn.execute(
        "SELECT COUNT(*) FROM code_relations WHERE source='import:codemap'"
    ).fetchone()[0]
    syms = conn.execute("SELECT COUNT(*) FROM code_symbols").fetchone()[0]
    assert rels == 0, "failed import left partial relations behind"
    assert syms == 0, "failed import left partial symbols behind"


def test_reimport_smaller_artifact_removes_stale_symbols(
    project, artifact_file, tmp_path,
):
    """Review #4 (low): re-importing a smaller artifact must remove
    symbols whose files/qnames vanished from the new artifact."""
    import_graph(get_db(), artifact_file)
    smaller = tmp_path / "smaller.json"
    smaller.write_text(json.dumps({
        "tool": "codemap",
        "nodes": [_ARTIFACT["nodes"][0]],
        "edges": [],
    }))
    import_graph(get_db(), smaller)
    syms = get_db().execute(
        "SELECT qualified_name FROM code_symbols"
    ).fetchall()
    qnames = {r[0] for r in syms}
    assert qnames == {"kt.Parser"}, (
        f"stale symbols survived smaller re-import: {qnames}"
    )


def test_summary_counts_match_written_rows(project, tmp_path):
    """Review #5 (low): artifact-internal duplicate edges must not
    inflate the summary counts."""
    artifact = tmp_path / "dupes.json"
    artifact.write_text(json.dumps({
        "tool": "codemap",
        "nodes": _ARTIFACT["nodes"][:2],
        "edges": [
            {"source": "n2", "target": "n1", "kind": "calls",
             "confidence": "verified", "line": 5},
            {"source": "n2", "target": "n1", "kind": "calls",
             "confidence": "verified", "line": 5},  # exact duplicate
        ],
    }))
    result = import_graph(get_db(), artifact)
    rows = get_db().execute(
        "SELECT COUNT(*) FROM code_relations"
    ).fetchone()[0]
    assert result["edges_resolved"] == rows == 1


def test_cli_tool_flag_value_not_positional(
    project, artifact_file, monkeypatch,
):
    """Review #6: `code import --tool x <file>` must treat x as the
    tool name, not as the artifact path."""
    from sage_memory.cli_code import run_code
    monkeypatch.setenv("SAGE_PROJECT_ROOT", str(project))
    rc = run_code([
        "import", "--tool", "clitool", str(artifact_file), "--json",
    ])
    assert rc == 0
    rels = _relations(get_db())
    assert rels and all(r["source"] == "import:clitool" for r in rels)
