"""T10a — ``sage-memory scan-codebase`` CLI shell tests.

Covers flag parsing, exit codes, summary surface, and the
missing-extra install-hint path. Most tests drive ``run_scan_codebase``
directly (no subprocess) because the function returns the int exit
code that the dispatch in ``__init__.py`` would propagate via
``sys.exit``.

The "extra missing" case is tested by monkeypatching the lazy import
inside ``run_scan_codebase`` — simulating an environment where
``tree_sitter_language_pack`` isn't installed without needing a
separate venv.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from sage_memory.cli_scan_codebase import run_scan_codebase, _format_summary
from sage_memory.codebase import ScanResult


# ---------------------------------------------------------------------------
# --help / argparse
# ---------------------------------------------------------------------------


def test_help_exits_zero(capsys) -> None:
    code = run_scan_codebase(["--help"])
    assert code == 0
    out = capsys.readouterr().out
    assert "sage-memory scan-codebase" in out
    assert "--languages" in out
    assert "--include-ignored" in out
    assert "--limit" in out
    assert "--force" in out
    assert "--dry-run" in out


def test_unknown_flag_exits_one(capsys) -> None:
    code = run_scan_codebase(["--no-such-flag"])
    assert code == 1


def test_nonexistent_path_exits_one(capsys) -> None:
    code = run_scan_codebase(["/path/that/definitely/does/not/exist"])
    assert code == 1
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_path_pointing_at_file_exits_one(tmp_path: Path, capsys) -> None:
    f = tmp_path / "regular_file.py"
    f.write_text("pass\n")
    code = run_scan_codebase([str(f)])
    assert code == 1
    err = capsys.readouterr().err
    assert "not a directory" in err


# ---------------------------------------------------------------------------
# Missing-extra → exit 2 with install hint
# ---------------------------------------------------------------------------


def test_missing_extra_exits_two_with_install_hint(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    """When the [codebase] extra is missing, the CLI must exit 2 and
    print the install hint to stderr. We simulate the absence by
    making ``scan()`` raise ``RuntimeError`` with the install-hint
    message — exactly what ``_require_extra()`` does in a real env
    without ``tree_sitter_language_pack`` on PYTHONPATH.
    """
    import sage_memory.codebase as codebase_mod

    def fake_scan(*args, **kwargs):
        raise RuntimeError(
            "scan-codebase requires the [codebase] extra:\n"
            "  pip install 'sage-memory[codebase]'"
        )

    monkeypatch.setattr(codebase_mod, "scan", fake_scan)

    code = run_scan_codebase([str(tmp_path)])
    assert code == 2
    err = capsys.readouterr().err
    assert "[codebase] extra" in err
    assert "pip install" in err


# ---------------------------------------------------------------------------
# Summary surface — spec-text matching against a synthetic ScanResult
# ---------------------------------------------------------------------------


def test_summary_includes_project_root_and_languages() -> None:
    result = ScanResult(
        project_root="/tmp/proj",
        languages_detected=["py", "ts"],
        files_scanned=10,
        files_changed=3,
        files_unchanged=7,
        relations_imports=5,
        relations_calls_resolved=20,
        relations_calls_unresolved=4,
        elapsed_ms=1234,
        symbols_by_kind={"FUNCTION": 8, "CLASS": 2, "METHOD": 4},
    )
    text = _format_summary(result)
    assert "project root: /tmp/proj" in text
    assert "languages: py, ts" in text
    assert "files: 10 scanned, 3 changed, 7 unchanged" in text
    # No error-nodes line when files_with_error_nodes == 0.
    assert "parse:" not in text
    assert "symbols: 8 functions, 2 classes, 4 methods" in text
    assert "relations: 5 imports, 24 calls (20 resolved, 4 unresolved)" in text
    assert "elapsed: 1.2s" in text


def test_summary_includes_parse_line_when_error_nodes_present() -> None:
    """Spec line 229: the ``parse:`` line is OMITTED when
    files_with_error_nodes == 0 — present otherwise.
    """
    result = ScanResult(
        project_root="/tmp",
        languages_detected=["py"],
        files_scanned=5,
        files_changed=5,
        files_with_error_nodes=2,
        relations_imports=0,
        relations_calls_resolved=0,
        relations_calls_unresolved=0,
        elapsed_ms=100,
    )
    text = _format_summary(result)
    assert "parse: 2 files had tree-sitter ERROR nodes" in text


def test_summary_dry_run_marker() -> None:
    result = ScanResult(
        project_root="/tmp",
        languages_detected=["py"],
        files_scanned=42,
        dry_run=True,
        elapsed_ms=50,
    )
    text = _format_summary(result)
    assert "dry-run: 42 files would be scanned" in text
    # No file-change/relation counters in dry-run mode.
    assert "scanned," not in text  # would appear in non-dry-run text
    assert "relations:" not in text


# ---------------------------------------------------------------------------
# End-to-end: scan a tiny project, verify summary + DB state
# ---------------------------------------------------------------------------
#
# These tests require the [codebase] extra. ``importorskip`` skips
# the entire module if absent. They also need an isolated DB so the
# real scan() call doesn't pollute the developer's local sage-memory
# database — we use SAGE_PROJECT_ROOT env var to point at tmp_path.


tsp = pytest.importorskip("tree_sitter_language_pack")


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch):
    """Set SAGE_PROJECT_ROOT to a clean tmp directory so scan() builds
    a per-test DB instead of touching ~/.sage-memory or the real
    project's DB. Also closes any cached connections from prior tests.
    """
    from sage_memory.db import close_all
    close_all()
    monkeypatch.setenv("SAGE_PROJECT_ROOT", str(tmp_path))
    # Ensure a marker file so find_project_root settles deterministically
    # if env var isn't honored for some reason.
    (tmp_path / "pyproject.toml").write_text("[tool.test]\n")
    yield tmp_path
    close_all()


def test_e2e_scan_runs_and_prints_summary(isolated_project, capsys) -> None:
    """Scan a tmp project with one python file; verify the CLI exits
    0 and the summary surface contains the expected counters.
    """
    (isolated_project / "main.py").write_text(
        "def helper(x):\n    return x + 1\n"
        "\n"
        "def caller():\n"
        "    return helper(1)\n"
    )
    code = run_scan_codebase([str(isolated_project)])
    out = capsys.readouterr().out
    assert code == 0
    assert "files: 1 scanned" in out
    assert "1 changed" in out
    # Two FUNCTION symbols expected (helper, caller).
    assert "2 functions" in out
    # One in-function call relation (caller → helper).
    assert "calls (1 resolved, 0 unresolved)" in out


def test_e2e_dry_run_writes_nothing(isolated_project, capsys) -> None:
    (isolated_project / "main.py").write_text("def f(): pass\n")
    code = run_scan_codebase([str(isolated_project), "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run: 1 files would be scanned" in out

    # No rows landed.
    from sage_memory.db import get_db
    conn = get_db("project")
    assert conn.execute(
        "SELECT COUNT(*) FROM code_symbols"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM codebase_scans"
    ).fetchone()[0] == 0


def test_e2e_languages_filter(isolated_project, capsys) -> None:
    (isolated_project / "main.py").write_text("def helper(): pass\n")
    (isolated_project / "main.ts").write_text(
        "function helper(): number { return 1; }\n"
    )
    code = run_scan_codebase(
        [str(isolated_project), "--languages", "py"]
    )
    assert code == 0
    out = capsys.readouterr().out
    # Only .py file scanned despite the .ts being present.
    assert "files: 1 scanned" in out
    assert "languages: py" in out


# ---------------------------------------------------------------------------
# T10b safety guards: --limit, home-dir refuse, advisory lock
# ---------------------------------------------------------------------------


def test_limit_exceeded_exits_three_no_writes(
    isolated_project, capsys,
) -> None:
    """``--limit 1`` with 2+ files → CLI prints the file count to
    stderr, exits 3, and DOES NOT write any rows to code_symbols or
    codebase_scans (rev 2 M3 contract).
    """
    (isolated_project / "a.py").write_text("def a(): pass\n")
    (isolated_project / "b.py").write_text("def b(): pass\n")

    code = run_scan_codebase([str(isolated_project), "--limit", "1"])
    assert code == 3
    err = capsys.readouterr().err
    assert "--limit 1 exceeded" in err
    assert "would scan 2 files" in err

    # Critical: zero side effects.
    from sage_memory.db import get_db
    conn = get_db("project")
    assert conn.execute("SELECT COUNT(*) FROM code_symbols").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM codebase_scans").fetchone()[0] == 0
    # Lock was never acquired either.
    assert conn.execute("SELECT COUNT(*) FROM scan_locks").fetchone()[0] == 0


def test_home_dir_refuse_exits_one(tmp_path: Path, capsys, monkeypatch) -> None:
    """Scanning ``$HOME`` is refused (rev 3 Minor #1). We override
    HOME to tmp_path and try to scan it.
    """
    # tmp_path is some `pytest-NNN/test_X` dir — pretend it IS home.
    monkeypatch.setenv("HOME", str(tmp_path))
    # Also clear SAGE_PROJECT_ROOT so resolve_project_root doesn't
    # intervene (we want the explicit path to be the home dir).
    monkeypatch.delenv("SAGE_PROJECT_ROOT", raising=False)
    (tmp_path / "main.py").write_text("def f(): pass\n")

    code = run_scan_codebase([str(tmp_path)])
    assert code == 1
    err = capsys.readouterr().err
    assert "Refusing to scan home directory" in err


def test_locked_db_scan_exits_one_with_already_in_progress_message(
    isolated_project, capsys,
) -> None:
    """Rev 3 Minor #5 deterministic test: pre-acquire the project's
    scan lock IN-TEST (not via subprocess), then run the CLI against
    the SAME project — the CLI must observe the lock and exit 1
    with the "scan already in progress" stderr message.
    """
    from sage_memory.codebase._lock import (
        acquire_scan_lock, release_scan_lock,
    )
    from sage_memory.db import get_db

    (isolated_project / "main.py").write_text("def f(): pass\n")

    # Plant a lock row under ANOTHER process's pid so our CLI run
    # (in this same process) can't satisfy the PRIMARY KEY constraint
    # but ALSO can't accidentally release it via the pid filter.
    conn = get_db("project")
    import os
    import time as _time
    conn.execute(
        "INSERT INTO scan_locks (lock_name, pid, started_at) "
        "VALUES (?, ?, ?)",
        ("scan", os.getpid() + 1, _time.time()),
    )
    conn.commit()

    try:
        code = run_scan_codebase([str(isolated_project)])
        assert code == 1
        err = capsys.readouterr().err
        assert "scan already in progress" in err
    finally:
        # Clean up the planted lock — release_scan_lock filters by
        # OUR pid and wouldn't touch the planted row, so DELETE
        # directly.
        conn.execute("DELETE FROM scan_locks")
        conn.commit()


def test_dry_run_skips_limit_and_lock_checks(
    isolated_project, capsys,
) -> None:
    """``--dry-run`` performs no writes and should NOT acquire the
    lock or refuse on --limit — users dry-running against a huge
    tree expect a count, not an error.
    """
    # Plant a fake lock (would block a real scan)
    from sage_memory.db import get_db
    conn = get_db("project")
    import os
    import time as _time
    conn.execute(
        "INSERT INTO scan_locks (lock_name, pid, started_at) "
        "VALUES (?, ?, ?)",
        ("scan", os.getpid() + 1, _time.time()),
    )
    conn.commit()

    (isolated_project / "a.py").write_text("def a(): pass\n")
    (isolated_project / "b.py").write_text("def b(): pass\n")
    (isolated_project / "c.py").write_text("def c(): pass\n")

    code = run_scan_codebase(
        [str(isolated_project), "--dry-run", "--limit", "1"],
    )
    assert code == 0  # NOT 3 — dry-run skips limit check
    out = capsys.readouterr().out
    assert "dry-run: 3 files would be scanned" in out

    # Clean up planted lock.
    conn.execute("DELETE FROM scan_locks")
    conn.commit()
