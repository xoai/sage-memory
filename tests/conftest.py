"""Shared pytest fixtures for the M1+ test modules.

Existing tests/test_all.py is self-contained and does NOT use these
fixtures — it predates the M1 build cycle. New M1 test modules
(test_migrations.py, test_embedder_cascade.py, test_status_cli.py,
test_embed_pending_meta.py) use the fixtures defined here.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec


# ───────────────────────────────────────────────────────────────────
# Migration directory + DB connection fixtures
# ───────────────────────────────────────────────────────────────────


PRODUCTION_MIGRATIONS_DIR = Path(__file__).parent.parent / "src" / "sage_memory" / "migrations"


@pytest.fixture
def tmp_migrations_dir(tmp_path: Path) -> Path:
    """A fresh tmp directory for migration files.

    The test populates it (typically by copying selected production
    migrations) and passes it to `_migrate(conn, migrations_dir=...)`.
    """
    d = tmp_path / "migrations"
    d.mkdir()
    return d


@pytest.fixture
def copy_production_migrations(tmp_migrations_dir: Path):
    """Helper: copy real migration files into tmp_migrations_dir.

    Usage in a test:
        def test_x(copy_production_migrations):
            copy_production_migrations("001_initial.sql", "002_edges.sql")
    """
    def _copy(*filenames: str) -> None:
        for name in filenames:
            src = PRODUCTION_MIGRATIONS_DIR / name
            assert src.exists(), f"production migration missing: {name}"
            shutil.copy2(src, tmp_migrations_dir / name)
    return _copy


@pytest.fixture
def fresh_db(tmp_path: Path) -> sqlite3.Connection:
    """A blank sqlite3 connection with sqlite_vec loaded, PRAGMA defaults
    matching production (`db.py:_open`), and NO migrations applied yet.
    Tests run `_migrate(conn, migrations_dir=...)` explicitly.
    """
    db_file = tmp_path / "fresh.db"
    conn = sqlite3.connect(str(db_file), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    yield conn
    conn.close()


# ───────────────────────────────────────────────────────────────────
# M4 (T0) — Shared FTS5 corpus for expand strong-signal tests
# ───────────────────────────────────────────────────────────────────


@pytest.fixture
def expand_corpus_db(tmp_path: Path, request):
    """Open a per-scenario sqlite DB pre-populated with one of the
    four spec A1 scenarios. Use as a parametrized fixture:

        @pytest.mark.parametrize("expand_corpus_db",
            ["strong", "high-top1-ambiguous", "ambiguous-all-weak",
             "low-confidence"], indirect=True)
        def test_x(expand_corpus_db): ...

    Or directly request a specific scenario via `indirect=True` with
    a single param.

    The fixture returns the open `sqlite3.Connection`. Cleanup closes
    it.
    """
    from fixtures.expand_corpus import build_scenario_db
    scenario_name = request.param
    db_path = tmp_path / f"expand_{scenario_name}.db"
    conn = build_scenario_db(scenario_name, db_path)
    yield conn
    conn.close()


@pytest.fixture
def bm25_probe():
    """Re-exports `fixtures.expand_corpus.bm25_probe` for test ergonomics.

    Returns the callable, NOT a pre-computed result — tests call it
    with their own (conn, query, limit) args.
    """
    from fixtures.expand_corpus import bm25_probe as _probe
    return _probe


# ── install-skills E2E fixtures (Task 8a) ─────────────────────────


@pytest.fixture
def tmp_install_root(tmp_path, monkeypatch):
    """Isolated home + cwd for install-skills end-to-end tests.

    Sets HOME and XDG_CONFIG_HOME to tmp_path subdirs so `--global`
    targets land in the tmp tree (never in the user's real config).
    Cwd is set to a `project/` subdir for `--project` tests.

    Returns an object with `.home`, `.xdg`, and `.project` attributes.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.chdir(project)

    class _Root:
        def __init__(self):
            self.home = home
            self.xdg = xdg
            self.project = project

    return _Root()


@pytest.fixture
def mock_stdin_decisions(monkeypatch):
    """Feed scripted Decision values to the conflict prompt.

    Usage:
        def test_x(mock_stdin_decisions, ...):
            mock_stdin_decisions(["o", "k", "s"])
            # next 3 prompts return OVERWRITE, KEEP, SKIP
    """
    def _factory(decisions):
        from sage_memory.install_skills import prompt
        it = iter(decisions)

        def _fake_prompt(*a, **kw):
            ch = next(it)
            return {
                "o": prompt.Decision.OVERWRITE,
                "k": prompt.Decision.KEEP,
                "s": prompt.Decision.SKIP,
            }[ch]

        monkeypatch.setattr(prompt, "prompt_conflict", _fake_prompt)

    return _factory
