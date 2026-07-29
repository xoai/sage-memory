"""P1-1 — Incremental rescan tests (SM-PERF-01, SM-PERF-03).

Written tests-first per 05-spec-phase1 §P1-1. The defect: a no-change
rescan re-read + re-parsed every file in `resolve_codebase` (measured
84.4s on the 458-file corpus at v0.13.1, ≈ the 73.2s cold scan).

Contracts pinned here:
  - No-change rescan performs ZERO parses (spy-verified)
  - Cross-file correctness: an unchanged file's unresolved relation
    resolves when a NEW file defines the target (fails under
    touched-only scoping — this is the dependent-set proof)
  - CASCADE-orphan restoration: when a changed file's symbols are
    deleted, resolved relations in OTHER files targeting them must
    survive (restored + re-resolved), not vanish
  - `force=True` re-parses everything (unchanged escape hatch)
  - `full_resolve=True` re-resolves from disk without re-scanning
  - Scoped counts preserved (T11 Major #1 behaviour)
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

tsp = pytest.importorskip("tree_sitter_language_pack")

from sage_memory.codebase import scan
from sage_memory.db import close_all, get_db


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch):
    close_all()
    monkeypatch.setenv("SAGE_PROJECT_ROOT", str(tmp_path))
    (tmp_path / "pyproject.toml").write_text("[tool.test]\n")
    yield tmp_path
    close_all()


_GO_CALLER = """package main

func main() {
\tFoo()
}
"""

_GO_DEF = """package main

func Foo() int {
\treturn 42
}
"""


def _relations(conn):
    return [
        dict(r) for r in conn.execute(
            "SELECT target_name, kind, confidence, target_symbol_id "
            "FROM code_relations"
        )
    ]


def test_no_change_rescan_does_zero_parses(isolated_project, monkeypatch):
    """Spy-verified: the second scan of an unchanged tree must not
    call the extractor at all — neither in _scan_file (content-hash
    skip) nor in resolve_codebase (DB-driven re-resolution)."""
    (isolated_project / "a.go").write_text(_GO_CALLER)
    (isolated_project / "b.go").write_text(_GO_DEF)
    scan(root=isolated_project)

    import sage_memory.codebase._extract as ext_mod
    import sage_memory.codebase._resolve as resolve_mod
    calls = []
    real_extract = ext_mod.extract

    def _spy(*args, **kwargs):
        calls.append(1)
        return real_extract(*args, **kwargs)

    # Patch BOTH bindings: _scan_file imports extract() at call time
    # from ._extract; _resolve binds it at module load.
    monkeypatch.setattr(ext_mod, "extract", _spy)
    monkeypatch.setattr(resolve_mod, "extract", _spy)
    scan(root=isolated_project)
    assert calls == [], (
        f"no-change rescan parsed {len(calls)} file(s); expected zero"
    )


def test_cross_file_dependent_resolution(isolated_project):
    """File A calls Foo; Foo undefined at first scan → unresolved.
    Add B defining Foo and rescan (A unchanged → not re-parsed) → A's
    relation must become resolved. Proves the dependent set is not
    narrowed to touched files."""
    (isolated_project / "a.go").write_text(_GO_CALLER)
    scan(root=isolated_project)

    conn = get_db()
    rels = _relations(conn)
    foo_calls = [r for r in rels if r["target_name"] == "Foo"]
    assert foo_calls, f"expected a Foo call relation; got {rels!r}"
    assert all(r["confidence"] == "unresolved" for r in foo_calls)

    (isolated_project / "b.go").write_text(_GO_DEF)
    scan(root=isolated_project)

    rels = _relations(conn)
    foo_calls = [r for r in rels if r["target_name"] == "Foo"]
    assert any(
        r["confidence"] == "resolved" and r["target_symbol_id"]
        for r in foo_calls
    ), f"dependent relation did not resolve; got {foo_calls!r}"


def test_cascade_orphan_relations_restored(isolated_project):
    """A calls Foo (resolved from B). Modify B → B's symbols are
    deleted, and A's relation row CASCADE-deletes via target FK.
    The scan must snapshot those orphan rows and re-resolve them —
    the relation must NOT vanish."""
    (isolated_project / "a.go").write_text(_GO_CALLER)
    (isolated_project / "b.go").write_text(_GO_DEF)
    scan(root=isolated_project)

    conn = get_db()
    assert any(
        r["target_name"] == "Foo" and r["confidence"] == "resolved"
        for r in _relations(conn)
    ), "setup: Foo call must be resolved after first scan"

    # Touch B (content change → re-scan → symbol delete + re-insert
    # with fresh ids → A's relation row CASCADE-deletes).
    (isolated_project / "b.go").write_text(_GO_DEF + "\nfunc Bar() {}\n")
    scan(root=isolated_project)

    foo_calls = [
        r for r in _relations(conn) if r["target_name"] == "Foo"
    ]
    assert any(
        r["confidence"] == "resolved" and r["target_symbol_id"]
        for r in foo_calls
    ), (
        f"orphan relation lost across B's re-scan; got {foo_calls!r}"
    )


def test_force_reextracts_everything(isolated_project, monkeypatch):
    """--force must keep today's full behavior: every file re-parsed."""
    (isolated_project / "a.go").write_text(_GO_CALLER)
    (isolated_project / "b.go").write_text(_GO_DEF)
    scan(root=isolated_project)

    import sage_memory.codebase._extract as ext_mod
    import sage_memory.codebase._resolve as resolve_mod
    calls = []
    real_extract = ext_mod.extract

    def _spy(*args, **kwargs):
        calls.append(1)
        return real_extract(*args, **kwargs)

    # Patch BOTH bindings: _scan_file imports extract() at call time
    # from ._extract; _resolve binds it at module load.
    monkeypatch.setattr(ext_mod, "extract", _spy)
    monkeypatch.setattr(resolve_mod, "extract", _spy)
    scan(root=isolated_project, force=True)
    assert len(calls) == 2, (
        f"force scan parsed {len(calls)} files; expected 2"
    )


def test_full_resolve_escape_hatch(isolated_project, monkeypatch):
    """full_resolve=True without force: scan skips unchanged files,
    but resolve re-extracts every file from disk (the pre-P1-1 path).
    """
    (isolated_project / "a.go").write_text(_GO_CALLER)
    (isolated_project / "b.go").write_text(_GO_DEF)
    scan(root=isolated_project)

    import sage_memory.codebase._extract as ext_mod
    import sage_memory.codebase._resolve as resolve_mod
    calls = []
    real_extract = ext_mod.extract

    def _spy(*args, **kwargs):
        calls.append(1)
        return real_extract(*args, **kwargs)

    # Patch BOTH bindings: _scan_file imports extract() at call time
    # from ._extract; _resolve binds it at module load.
    monkeypatch.setattr(ext_mod, "extract", _spy)
    monkeypatch.setattr(resolve_mod, "extract", _spy)
    scan(root=isolated_project, full_resolve=True)
    assert len(calls) == 2, (
        f"full_resolve re-extracted {len(calls)} files; expected 2"
    )


def test_scoped_counts_preserved(isolated_project):
    """T11 Major #1 contract still holds under the incremental path:
    a subdirectory rescan reports counts for that scope only."""
    a_dir = isolated_project / "a"
    a_dir.mkdir()
    (a_dir / "alpha.go").write_text(
        "package a\n\nfunc A1() {}\nfunc A2() {}\n"
    )
    b_dir = isolated_project / "b"
    b_dir.mkdir()
    (b_dir / "beta.go").write_text("package b\n\nfunc B1() {}\n")

    result_a = scan(root=a_dir)
    assert result_a.symbols_by_kind.get("FUNCTION") == 2

    result_b = scan(root=b_dir)
    assert result_b.symbols_by_kind.get("FUNCTION") == 1, (
        f"aggregation leaked: {result_b.symbols_by_kind}"
    )

    # No-change rescan of B: counts still scoped, and now fast.
    result_b2 = scan(root=b_dir)
    assert result_b2.symbols_by_kind.get("FUNCTION") == 1


@pytest.mark.perf
def test_no_change_rescan_under_five_seconds(isolated_project):
    """Perf budget (deselected by default; run with `-m perf`):
    no-change rescan of a 200-file fixture repo completes < 5s."""
    for i in range(200):
        (isolated_project / f"pkg{i % 10}").mkdir(exist_ok=True)
        (isolated_project / f"pkg{i % 10}" / f"file{i}.go").write_text(
            f"package pkg{i % 10}\n\n"
            f"func Fn{i}() int {{\n\treturn Other{i % 50}()\n}}\n\n"
            f"func Other{i % 50}() int {{\n\treturn {i}\n}}\n"
        )
    scan(root=isolated_project)

    start = time.perf_counter()
    scan(root=isolated_project)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, (
        f"no-change rescan took {elapsed:.2f}s; budget < 5s"
    )
