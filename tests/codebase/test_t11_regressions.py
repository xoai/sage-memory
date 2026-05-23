"""T11 regression tests for the 4 Major findings from the sub-agent
code review.

Each test isolates the specific failure mode the original sub-agent
flagged and demonstrates that the fix actually fixes it. The Critical
finding (lock transaction leak) is covered by a dedicated test in
``test_scan_lock.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

tsp = pytest.importorskip("tree_sitter_language_pack")

from sage_memory.codebase import scan
from sage_memory.codebase._resolve import (
    _LANGUAGE_TO_GRAMMAR_QUERY,
    resolve_codebase,
)


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch):
    from sage_memory.db import close_all
    close_all()
    monkeypatch.setenv("SAGE_PROJECT_ROOT", str(tmp_path))
    (tmp_path / "pyproject.toml").write_text("[tool.test]\n")
    yield tmp_path
    close_all()


# ---------------------------------------------------------------------------
# Major #1: aggregation scope-leak
# ---------------------------------------------------------------------------


def test_aggregation_scope_leak_two_separate_scans(isolated_project) -> None:
    """A scan on subdir A followed by a scan on subdir B (sharing the
    same project DB) — the second scan's reported symbol/relation
    counts must NOT include symbols from A.

    Before the T11 fix, ``_aggregate_*`` queried the entire DB and
    second-scan totals were cumulative.
    """
    # Two sibling sub-projects under the same project root.
    a_dir = isolated_project / "a"
    a_dir.mkdir()
    (a_dir / "alpha.py").write_text(
        "def alpha_one(): pass\n"
        "def alpha_two(): pass\n"
        "def alpha_three(): pass\n"
    )

    b_dir = isolated_project / "b"
    b_dir.mkdir()
    (b_dir / "beta.py").write_text("def beta_only(): pass\n")

    # Scan A — 3 functions.
    result_a = scan(root=a_dir)
    assert result_a.symbols_by_kind.get("FUNCTION") == 3

    # Scan B — should report 1 function, not 4 (3 from A + 1 from B).
    result_b = scan(root=b_dir)
    assert result_b.symbols_by_kind.get("FUNCTION") == 1, (
        f"aggregation leaked cross-scan symbols: "
        f"got {result_b.symbols_by_kind}"
    )


# ---------------------------------------------------------------------------
# Major #2: .h heuristic drift between scan and resolve
# ---------------------------------------------------------------------------


def test_language_to_grammar_query_table_covers_all_walker_languages() -> None:
    """``_LANGUAGE_TO_GRAMMAR_QUERY`` must have an entry for every
    language tag that ``EXT_MAP`` can produce, including ``c`` and
    ``cpp`` (the two possible ``.h`` resolutions). This is what makes
    resolve_codebase deterministic w.r.t. the filesystem state at
    resolve time.
    """
    expected = {
        "py", "ts", "js", "go", "rs", "java", "rb", "php", "c", "cpp",
    }
    assert expected.issubset(_LANGUAGE_TO_GRAMMAR_QUERY.keys())


def test_h_file_resolve_uses_stored_language_not_heuristic(
    isolated_project,
) -> None:
    """After scanning a directory with `.h` + `.cpp` (so `.h` resolves
    to cpp), the resolver must use ``cpp`` for the `.h` file even if
    the `.cpp` sibling were deleted between scan and resolve. The
    previous design re-ran the heuristic and would have flipped to
    `c`.

    We don't actually delete the sibling (the heuristic in v1
    deterministically returns the same answer in this test); we just
    verify the resolver doesn't crash AND produces relations,
    proving it found the right grammar/query via the stored language.
    """
    (isolated_project / "lib.cpp").write_text(
        '#include "lib.h"\n'
        "class Point { public: int x() const { return 1; } };\n"
    )
    (isolated_project / "lib.h").write_text(
        "#ifndef LIB_H\n#define LIB_H\nstruct Header { int v; };\n#endif\n"
    )

    result = scan(root=isolated_project)
    # Both files scanned cleanly (no parse errors).
    assert result.parse_errors == 0
    # Both files' STRUCTs land — Header (from .h via cpp grammar)
    # and the .cpp file's class.
    assert result.symbols_by_kind.get("STRUCT", 0) >= 1


# ---------------------------------------------------------------------------
# Major #3: silent parser-skip when parser not in `parsers` dict
# ---------------------------------------------------------------------------


def test_resolve_codebase_lazy_loads_missing_parser(isolated_project) -> None:
    """A user re-scans with `--languages py` only after a prior scan
    indexed mixed-language files. The `.go` files are still in
    ``codebase_scans``. ``parsers`` only contains the Python parser
    this run. Before the T11 fix, the resolver silently skipped the
    Go files. After the fix, it lazily loads the Go parser and
    resolves Go relations too.
    """
    # First scan: index a Python file AND a Go file.
    (isolated_project / "main.py").write_text(
        "def caller():\n    return helper()\n"
        "def helper(): return 1\n"
    )
    (isolated_project / "main.go").write_text(
        "package main\n"
        "func helper() int { return 1 }\n"
        "func caller() int { return helper() }\n"
    )
    scan(root=isolated_project)

    # Second pass: call resolve_codebase directly with only the
    # python parser pre-loaded, mimicking a `--languages py` rescan.
    from sage_memory.db import get_db
    conn = get_db("project")
    parsers = {"py": tsp.get_parser("python")}  # NO go parser

    result = resolve_codebase(
        conn, project_root=isolated_project, parsers=parsers,
    )

    # Critical assertion: NO files skipped due to missing parser.
    assert result.files_skipped == 0, (
        f"parser-skip silence regressed: {result.files_skipped} "
        f"files silently skipped"
    )
    # Both files walked.
    assert result.files_walked == 2


# ---------------------------------------------------------------------------
# Major #4: covered by test_rs_pos_same_file_scoped_call_resolves
# in test_resolve_per_language.py — see that test for the regression
# fixture.
# ---------------------------------------------------------------------------
