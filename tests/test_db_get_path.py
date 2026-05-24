"""M3.0 — ``db.get_db_path(conn) -> Path`` helper.

Per plan M3.0 (promoted from M3.2 footnote per architecture-review
MAJOR-1): store.py's per-DB ownership check needs to resolve "which
project DB does this connection belong to?" — neither
``get_global_db_path`` nor ``get_project_db_path`` does this (they
map root → path, not connection → path).

3 tests per plan.md M3.0 done-when:
  1. returns absolute path for the project DB
  2. returns global DB path for the global connection
  3. returns the same path for two connections opened against the same DB
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sage_memory.db import (
    _open, close_all, get_db_path, get_global_db_path,
    get_project_db_path, override_project_root,
)


@pytest.fixture
def isolated_db_state():
    """Tear down sage-memory's connection cache before AND after each
    test so per-test DB paths don't leak."""
    close_all()
    yield
    close_all()


def test_get_db_path_returns_absolute_project_db_path(
    isolated_db_state, tmp_path,
):
    project_root = tmp_path / "myproject"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    expected = get_project_db_path(project_root)
    conn = _open(expected)
    assert get_db_path(conn) == expected.resolve(), (
        f"get_db_path should return the project DB's resolved path; "
        f"got {get_db_path(conn)!r}; expected {expected.resolve()!r}"
    )


def test_get_db_path_returns_global_db_path(
    isolated_db_state, tmp_path, monkeypatch,
):
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    expected = get_global_db_path()
    conn = _open(expected)
    assert get_db_path(conn) == expected.resolve(), (
        f"get_db_path on the global connection should return the "
        f"global DB path; got {get_db_path(conn)!r}; "
        f"expected {expected.resolve()!r}"
    )


def test_get_db_path_returns_same_path_for_cached_connection(
    isolated_db_state, tmp_path,
):
    """Sage-memory's _open() caches connections keyed by path. Two
    calls with the same path return the same connection object —
    get_db_path on either must return the same Path."""
    project_root = tmp_path / "myproject"
    project_root.mkdir()
    db_path = get_project_db_path(project_root)
    conn_a = _open(db_path)
    conn_b = _open(db_path)
    # The cache returns the same object; both paths must match.
    assert conn_a is conn_b, "_open should return cached connection"
    assert get_db_path(conn_a) == get_db_path(conn_b)
    assert get_db_path(conn_a) == db_path.resolve()
