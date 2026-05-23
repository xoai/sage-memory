"""T2 — Migration 009 (code_symbols + code_relations + codebase_scans +
scan_locks) + re-apply tests.

Covers spec rev 3 schema additions:
  - 4 tables present + indexes.
  - PRAGMA foreign_keys = 1 enforced (set by db._open via fresh_db).
  - Self-referential CASCADE on code_symbols.parent_id.
  - UNIQUE-discriminator behavior — line_start for code_symbols,
    column_start for code_relations (rev 2 sibling-shadow + same-line
    duplicate call cases).
  - Re-apply no-op test (plan rev 3 Minor #1 fix: uses fresh_db for
    initial + sqlite3.connect for the reopen step, NOT db._open which
    caches connections by path and auto-runs production _migrate).
  - Re-apply with-data test (rev 1 Minor #3 fix).
"""

from __future__ import annotations

import sqlite3
import time
import uuid

import pytest

from sage_memory.db import _migrate


ALL_MIGRATIONS = (
    "001_initial.sql",
    "002_edges.sql",
    "003_memory_health.sql",
    "004_chunks.sql",
    "005_entities.sql",
    "006_embedding_meta.sql",
    "007_extraction_queue.sql",
    "008_worker_state.sql",
    "009_code_symbols.sql",
)


def _apply_all(fresh_db, tmp_migrations_dir, copy_production_migrations):
    """Apply migrations 001-009 to a fresh DB."""
    copy_production_migrations(*ALL_MIGRATIONS)
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)


def _insert_memory(conn: sqlite3.Connection, memory_id: str) -> None:
    """Helper: minimal memories row so code_symbols FK is satisfiable."""
    now = time.time()
    conn.execute(
        "INSERT INTO memories (id, title, content, content_hash, "
        "created_at, updated_at, accessed_at) "
        "VALUES (?, 'f', 'f', ?, ?, ?, ?)",
        (memory_id, str(uuid.uuid4()), now, now, now),
    )


# ── Schema presence ───────────────────────────────────────────────


def test_migration_009_creates_all_tables(
    fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    _apply_all(fresh_db, tmp_migrations_dir, copy_production_migrations)

    tables = {row[0] for row in fresh_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}

    for required in (
        "code_symbols",
        "code_relations",
        "codebase_scans",
        "scan_locks",
    ):
        assert required in tables, f"missing table: {required}"


def test_migration_009_creates_indexes(
    fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    _apply_all(fresh_db, tmp_migrations_dir, copy_production_migrations)

    indexes = {row[0] for row in fresh_db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name LIKE 'idx_%'"
    )}

    for required in (
        "idx_code_symbols_name",
        "idx_code_symbols_qualified",
        "idx_code_symbols_file",
        "idx_code_symbols_lang_kind",
        "idx_code_relations_src",
        "idx_code_relations_tgt",
        "idx_code_relations_kind",
        "idx_codebase_scans_lang",
    ):
        assert required in indexes, f"missing index: {required}"


def test_user_version_advances_to_9(
    fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    _apply_all(fresh_db, tmp_migrations_dir, copy_production_migrations)
    assert fresh_db.execute("PRAGMA user_version").fetchone()[0] == 9


def test_foreign_keys_enforced_by_fresh_db(fresh_db):
    """The fresh_db fixture mirrors db._open by setting foreign_keys ON
    at connection time. Self-ref CASCADE relies on this PRAGMA — flag
    if a future fixture change breaks it.
    """
    assert fresh_db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# ── Self-ref CASCADE ──────────────────────────────────────────────


def test_self_ref_cascade_drops_child_when_parent_deleted(
    fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    """parent_id is a self-ref FK with ON DELETE CASCADE — when a
    parent symbol is deleted (e.g., file re-scanned), nested children
    (methods inside a class, inner fns) drop atomically.
    """
    _apply_all(fresh_db, tmp_migrations_dir, copy_production_migrations)

    mem_id = str(uuid.uuid4())
    _insert_memory(fresh_db, mem_id)

    now = time.time()
    parent_id = str(uuid.uuid4())
    child_id = str(uuid.uuid4())

    fresh_db.execute(
        "INSERT INTO code_symbols "
        "(id, file_memory_id, name, qualified_name, kind, language, "
        " line_start, line_end, parent_id, created_at) "
        "VALUES (?, ?, 'outer', 'outer', 'FUNCTION', 'py', 1, 5, NULL, ?)",
        (parent_id, mem_id, now),
    )
    fresh_db.execute(
        "INSERT INTO code_symbols "
        "(id, file_memory_id, name, qualified_name, kind, language, "
        " line_start, line_end, parent_id, created_at) "
        "VALUES (?, ?, 'inner', 'outer.inner', 'FUNCTION', 'py', 2, 3, ?, ?)",
        (child_id, mem_id, parent_id, now),
    )

    assert fresh_db.execute(
        "SELECT COUNT(*) FROM code_symbols"
    ).fetchone()[0] == 2

    fresh_db.execute("DELETE FROM code_symbols WHERE id=?", (parent_id,))

    assert fresh_db.execute(
        "SELECT COUNT(*) FROM code_symbols"
    ).fetchone()[0] == 0


# ── UNIQUE discriminators ─────────────────────────────────────────


def test_code_symbols_unique_includes_line_start(
    fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    """Rev 2 fix: UNIQUE adds line_start so sibling-shadow nested fns
    (two `def outer():` each with `def inner_helper()`) at different
    lines don't collide on (file_memory_id, qualified_name, kind).
    """
    _apply_all(fresh_db, tmp_migrations_dir, copy_production_migrations)

    mem_id = str(uuid.uuid4())
    _insert_memory(fresh_db, mem_id)

    now = time.time()
    common = dict(
        file_memory_id=mem_id, name="inner_helper",
        qualified_name="outer.inner_helper", kind="FUNCTION",
        language="py", line_end=10, parent_id=None, created_at=now,
    )

    # Both should INSERT successfully — different line_start.
    fresh_db.execute(
        "INSERT INTO code_symbols "
        "(id, file_memory_id, name, qualified_name, kind, language, "
        " line_start, line_end, parent_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 5, ?, ?, ?)",
        (str(uuid.uuid4()), common["file_memory_id"], common["name"],
         common["qualified_name"], common["kind"], common["language"],
         common["line_end"], common["parent_id"], common["created_at"]),
    )
    fresh_db.execute(
        "INSERT INTO code_symbols "
        "(id, file_memory_id, name, qualified_name, kind, language, "
        " line_start, line_end, parent_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 20, ?, ?, ?)",
        (str(uuid.uuid4()), common["file_memory_id"], common["name"],
         common["qualified_name"], common["kind"], common["language"],
         common["line_end"], common["parent_id"], common["created_at"]),
    )

    # Two rows landed.
    assert fresh_db.execute(
        "SELECT COUNT(*) FROM code_symbols WHERE qualified_name=?",
        (common["qualified_name"],),
    ).fetchone()[0] == 2

    # Exact same line_start — should fail.
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute(
            "INSERT INTO code_symbols "
            "(id, file_memory_id, name, qualified_name, kind, language, "
            " line_start, line_end, parent_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 5, ?, ?, ?)",
            (str(uuid.uuid4()), common["file_memory_id"], common["name"],
             common["qualified_name"], common["kind"], common["language"],
             common["line_end"], common["parent_id"], common["created_at"]),
        )


def test_code_relations_unique_includes_column_start(
    fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    """Rev 2 fix: UNIQUE adds column_start so same-line duplicate calls
    (`foo(); foo()` on one line) at different columns don't collide.
    """
    _apply_all(fresh_db, tmp_migrations_dir, copy_production_migrations)

    mem_id = str(uuid.uuid4())
    _insert_memory(fresh_db, mem_id)
    now = time.time()

    src_id = str(uuid.uuid4())
    fresh_db.execute(
        "INSERT INTO code_symbols "
        "(id, file_memory_id, name, qualified_name, kind, language, "
        " line_start, line_end, parent_id, created_at) "
        "VALUES (?, ?, 'caller', 'caller', 'FUNCTION', 'py', 1, 5, NULL, ?)",
        (src_id, mem_id, now),
    )

    # Two relations at same line different cols — both succeed.
    fresh_db.execute(
        "INSERT INTO code_relations "
        "(id, source_symbol_id, target_symbol_id, target_name, kind, "
        " confidence, line, column_start, created_at) "
        "VALUES (?, ?, NULL, 'foo', 'calls', 'unresolved', 7, 0, ?)",
        (str(uuid.uuid4()), src_id, now),
    )
    fresh_db.execute(
        "INSERT INTO code_relations "
        "(id, source_symbol_id, target_symbol_id, target_name, kind, "
        " confidence, line, column_start, created_at) "
        "VALUES (?, ?, NULL, 'foo', 'calls', 'unresolved', 7, 7, ?)",
        (str(uuid.uuid4()), src_id, now),
    )

    assert fresh_db.execute(
        "SELECT COUNT(*) FROM code_relations WHERE source_symbol_id=?",
        (src_id,),
    ).fetchone()[0] == 2

    # Same line AND column — fails.
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute(
            "INSERT INTO code_relations "
            "(id, source_symbol_id, target_symbol_id, target_name, kind, "
            " confidence, line, column_start, created_at) "
            "VALUES (?, ?, NULL, 'foo', 'calls', 'unresolved', 7, 0, ?)",
            (str(uuid.uuid4()), src_id, now),
        )


# ── Re-apply tests (plan rev 3 Minor #1 + #3 fixes) ───────────────


def _reopen_with_pragmas(db_path) -> sqlite3.Connection:
    """Reopen a sqlite DB file with the same PRAGMAs db._open sets.

    Rev 3 Minor #1 fix: we deliberately do NOT use db._open() here —
    that function caches connections by path AND auto-runs the
    PRODUCTION migrations directory, which would defeat the
    `_migrate(reopened, migrations_dir=tmp_migrations_dir)` test by
    advancing user_version before we get a chance to re-call _migrate.
    """
    import sqlite_vec

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def test_reapply_migration_009_is_noop(
    tmp_path, fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    """Re-running _migrate on an already-migrated DB MUST NOT re-run 009
    (user_version gate). The gate is what makes the migration runner
    safe to call on every connection open in production.
    """
    _apply_all(fresh_db, tmp_migrations_dir, copy_production_migrations)
    # fresh_db's underlying file:
    db_path = tmp_path / "fresh.db"

    # Sanity: user_version == 9 after first run.
    assert fresh_db.execute("PRAGMA user_version").fetchone()[0] == 9
    fresh_db.close()

    # Reopen with a brand-new connection (NOT db._open — see helper).
    reopened = _reopen_with_pragmas(db_path)
    try:
        # Second _migrate against same migrations_dir.
        _migrate(reopened, migrations_dir=tmp_migrations_dir)
        # user_version still 9, not 10, not rolled back to 0.
        assert reopened.execute("PRAGMA user_version").fetchone()[0] == 9
        # All tables still present (no DROP/CREATE churn).
        tables = {row[0] for row in reopened.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        for t in ("code_symbols", "code_relations",
                  "codebase_scans", "scan_locks"):
            assert t in tables
    finally:
        reopened.close()


def test_reapply_migration_009_with_data_preserves_rows_and_cascade(
    tmp_path, fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    """Re-applying 009 with EXISTING code_symbols rows must not lose
    data, AND the self-ref CASCADE must remain active afterwards
    (rev 1 Minor #3 fix).
    """
    _apply_all(fresh_db, tmp_migrations_dir, copy_production_migrations)
    db_path = tmp_path / "fresh.db"

    mem_id = str(uuid.uuid4())
    _insert_memory(fresh_db, mem_id)

    parent_id = str(uuid.uuid4())
    child_id = str(uuid.uuid4())
    now = time.time()
    fresh_db.execute(
        "INSERT INTO code_symbols "
        "(id, file_memory_id, name, qualified_name, kind, language, "
        " line_start, line_end, parent_id, created_at) "
        "VALUES (?, ?, 'A', 'A', 'CLASS', 'py', 1, 10, NULL, ?)",
        (parent_id, mem_id, now),
    )
    fresh_db.execute(
        "INSERT INTO code_symbols "
        "(id, file_memory_id, name, qualified_name, kind, language, "
        " line_start, line_end, parent_id, created_at) "
        "VALUES (?, ?, 'A.m', 'A.m', 'METHOD', 'py', 2, 5, ?, ?)",
        (child_id, mem_id, parent_id, now),
    )
    fresh_db.commit()
    fresh_db.close()

    # Reopen + re-migrate.
    reopened = _reopen_with_pragmas(db_path)
    try:
        _migrate(reopened, migrations_dir=tmp_migrations_dir)

        # Rows survived.
        assert reopened.execute(
            "SELECT COUNT(*) FROM code_symbols"
        ).fetchone()[0] == 2

        # CASCADE still active on the reopened connection.
        reopened.execute("DELETE FROM code_symbols WHERE id=?", (parent_id,))
        reopened.commit()
        assert reopened.execute(
            "SELECT COUNT(*) FROM code_symbols"
        ).fetchone()[0] == 0
    finally:
        reopened.close()
