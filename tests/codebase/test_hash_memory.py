"""T4 — hash module + file-memory upsert tests.

Two-part coverage:

1. ``_hash.py`` — ``file_content_hash`` (unsalted; drives delta-rescan
   via ``codebase_scans``) and ``memory_content_hash`` (path-salted;
   stored in ``memories.content_hash`` so multiple empty ``__init__.py``
   at distinct relative paths don't collide on the ``UNIQUE`` constraint).

2. ``upsert_file_memory(conn, abs_path, rel_path, language) -> memory_id``
   — takes a raw ``sqlite3.Connection`` (rev 3 C1 fix: no DB wrapper),
   writes only to the ``memories`` table, leaves ``codebase_scans`` (and
   ``code_symbols`` / ``code_relations`` / ``scan_locks``) untouched —
   that's T5's job. Idempotent on identical content.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from pathlib import Path

import pytest

from sage_memory.codebase import upsert_file_memory
from sage_memory.codebase._hash import file_content_hash, memory_content_hash
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


@pytest.fixture
def migrated_db(fresh_db, tmp_migrations_dir, copy_production_migrations):
    """fresh_db with all 9 production migrations applied."""
    copy_production_migrations(*ALL_MIGRATIONS)
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    return fresh_db


# ---------------------------------------------------------------------------
# file_content_hash — unsalted, for delta-rescan via codebase_scans
# ---------------------------------------------------------------------------


def test_file_content_hash_matches_plain_sha256() -> None:
    data = b"def f():\n    return 1\n"
    assert file_content_hash(data) == hashlib.sha256(data).hexdigest()


def test_file_content_hash_deterministic() -> None:
    data = b"hello world"
    assert file_content_hash(data) == file_content_hash(data)


def test_file_content_hash_different_for_different_bytes() -> None:
    assert file_content_hash(b"a") != file_content_hash(b"b")


def test_file_content_hash_handles_empty_bytes() -> None:
    # Empty __init__.py: same unsalted hash regardless of path.
    assert file_content_hash(b"") == hashlib.sha256(b"").hexdigest()


def test_file_content_hash_handles_non_utf8_bytes() -> None:
    # Binary blob mistakenly matching a known extension should not raise.
    data = bytes(range(256))
    assert file_content_hash(data) == hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# memory_content_hash — path-salted, prevents UNIQUE collisions
# ---------------------------------------------------------------------------


def test_memory_content_hash_deterministic() -> None:
    assert memory_content_hash("a/b.py", b"x") == memory_content_hash("a/b.py", b"x")


def test_memory_content_hash_two_empty_init_py_at_different_paths() -> None:
    """The canonical reason for salting: many empty __init__.py in a
    Python project would collide on memories.content_hash UNIQUE.
    """
    h1 = memory_content_hash("pkg/__init__.py", b"")
    h2 = memory_content_hash("pkg/sub/__init__.py", b"")
    assert h1 != h2


def test_memory_content_hash_changes_with_content() -> None:
    h1 = memory_content_hash("a.py", b"v1")
    h2 = memory_content_hash("a.py", b"v2")
    assert h1 != h2


def test_memory_content_hash_differs_from_unsalted_file_hash() -> None:
    """Documented invariant from spec §"Note on content_hash semantics":
    memories.content_hash (salted) is NOT comparable to
    codebase_scans.content_hash (unsalted file hash).
    """
    rel, data = "a.py", b"x"
    assert memory_content_hash(rel, data) != file_content_hash(data)


def test_memory_content_hash_uses_documented_format() -> None:
    """Spec line 145: sha256(f"file:{relative_path}:{file_bytes}")."""
    rel, data = "src/a.py", b"hello"
    expected = hashlib.sha256(
        b"file:" + rel.encode("utf-8") + b":" + data
    ).hexdigest()
    assert memory_content_hash(rel, data) == expected


# ---------------------------------------------------------------------------
# upsert_file_memory — basic shape
# ---------------------------------------------------------------------------


def test_upsert_file_memory_creates_row_and_returns_id(
    migrated_db: sqlite3.Connection, tmp_path: Path,
) -> None:
    abs_path = tmp_path / "main.py"
    abs_path.write_text("print('hi')\n")
    memory_id = upsert_file_memory(
        migrated_db, abs_path=abs_path, rel_path="main.py", language="py",
    )
    assert isinstance(memory_id, str) and memory_id  # uuid hex
    row = migrated_db.execute(
        "SELECT id, title, content, tags, content_hash FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    assert row is not None
    assert row["id"] == memory_id


def test_upsert_file_memory_title_format(
    migrated_db: sqlite3.Connection, tmp_path: Path,
) -> None:
    abs_path = tmp_path / "src" / "main.py"
    abs_path.parent.mkdir()
    abs_path.write_text("x = 1\n")
    memory_id = upsert_file_memory(
        migrated_db, abs_path=abs_path, rel_path="src/main.py", language="py",
    )
    row = migrated_db.execute(
        "SELECT title FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    # Spec line 142: `[file:<lang>] path/relative/to/project/root/foo.py`
    assert row["title"] == "[file:py] src/main.py"


def test_upsert_file_memory_tags_format(
    migrated_db: sqlite3.Connection, tmp_path: Path,
) -> None:
    abs_path = tmp_path / "a.py"
    abs_path.write_text("")
    memory_id = upsert_file_memory(
        migrated_db, abs_path=abs_path, rel_path="a.py", language="py",
    )
    row = migrated_db.execute(
        "SELECT tags FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    import json
    tags = json.loads(row["tags"])
    # Spec line 144: ["codebase", "file", "<lang>"]
    assert set(tags) == {"codebase", "file", "py"}


def test_upsert_file_memory_content_mentions_language_label(
    migrated_db: sqlite3.Connection, tmp_path: Path,
) -> None:
    """Content includes the human-friendly language label so FTS5 can
    surface files by language keyword ('Python', 'TypeScript', etc.).
    Spec line 143 example: 'Source file (Python) — ...'.
    """
    abs_path = tmp_path / "a.ts"
    abs_path.write_text("")
    memory_id = upsert_file_memory(
        migrated_db, abs_path=abs_path, rel_path="a.ts", language="ts",
    )
    row = migrated_db.execute(
        "SELECT content FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    assert "TypeScript" in row["content"]


def test_upsert_file_memory_uses_salted_hash(
    migrated_db: sqlite3.Connection, tmp_path: Path,
) -> None:
    abs_path = tmp_path / "a.py"
    abs_path.write_text("x = 1\n")
    memory_id = upsert_file_memory(
        migrated_db, abs_path=abs_path, rel_path="a.py", language="py",
    )
    row = migrated_db.execute(
        "SELECT content_hash FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    expected = memory_content_hash("a.py", b"x = 1\n")
    assert row["content_hash"] == expected


# ---------------------------------------------------------------------------
# upsert_file_memory — idempotency and content-change behavior
# ---------------------------------------------------------------------------


def test_upsert_file_memory_idempotent_on_same_content(
    migrated_db: sqlite3.Connection, tmp_path: Path,
) -> None:
    """Calling upsert twice with identical bytes at the same path returns
    the same memory_id and produces exactly one row.
    """
    abs_path = tmp_path / "a.py"
    abs_path.write_text("v1\n")
    id1 = upsert_file_memory(
        migrated_db, abs_path=abs_path, rel_path="a.py", language="py",
    )
    id2 = upsert_file_memory(
        migrated_db, abs_path=abs_path, rel_path="a.py", language="py",
    )
    assert id1 == id2
    count = migrated_db.execute(
        "SELECT COUNT(*) FROM memories WHERE id = ?", (id1,)
    ).fetchone()[0]
    assert count == 1


def test_upsert_file_memory_two_empty_init_py_at_different_paths(
    migrated_db: sqlite3.Connection, tmp_path: Path,
) -> None:
    """Two empty __init__.py at distinct rel_paths both upsert
    successfully — this is the canonical scenario the salted hash exists
    to support. Without salting, the second call would violate the
    memories.content_hash UNIQUE constraint.
    """
    a = tmp_path / "pkg" / "__init__.py"
    a.parent.mkdir()
    a.write_text("")
    b = tmp_path / "pkg" / "sub" / "__init__.py"
    b.parent.mkdir(parents=True)
    b.write_text("")

    id_a = upsert_file_memory(
        migrated_db, abs_path=a, rel_path="pkg/__init__.py", language="py",
    )
    id_b = upsert_file_memory(
        migrated_db, abs_path=b, rel_path="pkg/sub/__init__.py", language="py",
    )
    assert id_a != id_b
    assert migrated_db.execute(
        "SELECT COUNT(*) FROM memories WHERE id IN (?, ?)", (id_a, id_b)
    ).fetchone()[0] == 2


def test_upsert_file_memory_different_content_creates_new_row(
    migrated_db: sqlite3.Connection, tmp_path: Path,
) -> None:
    """When the same file's bytes change, the salted hash changes →
    upsert creates a NEW memory row with a new id. Cleaning up the old
    row is T5's responsibility (it looks up the old memory_id via
    codebase_scans by abs_path).
    """
    abs_path = tmp_path / "a.py"
    abs_path.write_text("v1\n")
    id1 = upsert_file_memory(
        migrated_db, abs_path=abs_path, rel_path="a.py", language="py",
    )
    abs_path.write_text("v2 — different bytes\n")
    id2 = upsert_file_memory(
        migrated_db, abs_path=abs_path, rel_path="a.py", language="py",
    )
    assert id1 != id2
    count = migrated_db.execute(
        "SELECT COUNT(*) FROM memories WHERE tags LIKE '%codebase%'"
    ).fetchone()[0]
    assert count == 2


# ---------------------------------------------------------------------------
# Scope discipline — T4 writes ONLY to memories
# ---------------------------------------------------------------------------


def test_upsert_file_memory_does_not_write_codebase_scans(
    migrated_db: sqlite3.Connection, tmp_path: Path,
) -> None:
    """Plan rev 3 C1 fix: T4 must not touch codebase_scans. That's the
    sole responsibility of T5's _scan_file() — making it the only
    writer prevents orphan-state poisoning if T5 fails mid-flight.
    """
    abs_path = tmp_path / "a.py"
    abs_path.write_text("x\n")
    upsert_file_memory(
        migrated_db, abs_path=abs_path, rel_path="a.py", language="py",
    )
    assert migrated_db.execute(
        "SELECT COUNT(*) FROM codebase_scans"
    ).fetchone()[0] == 0


def test_upsert_file_memory_does_not_write_code_symbols_or_relations(
    migrated_db: sqlite3.Connection, tmp_path: Path,
) -> None:
    abs_path = tmp_path / "a.py"
    abs_path.write_text("x\n")
    upsert_file_memory(
        migrated_db, abs_path=abs_path, rel_path="a.py", language="py",
    )
    assert migrated_db.execute("SELECT COUNT(*) FROM code_symbols").fetchone()[0] == 0
    assert migrated_db.execute("SELECT COUNT(*) FROM code_relations").fetchone()[0] == 0
    assert migrated_db.execute("SELECT COUNT(*) FROM scan_locks").fetchone()[0] == 0


def test_upsert_file_memory_accepts_raw_sqlite3_connection(
    tmp_path: Path,
) -> None:
    """Plan rev 3 C1 fix: signature takes raw sqlite3.Connection, not
    the project DB wrapper. T5 opens `with conn:` for atomic-per-file
    transactions; that requires the raw connection.

    Sanity-check the contract by building a bare sqlite3 connection
    (not the fresh_db fixture's preconfigured one) and applying just
    enough schema to satisfy the FK.
    """
    db_file = tmp_path / "raw.db"
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Minimum schema: memories table only. T4 does not touch the FTS5
    # mirror or the vec0 virtual table for its writes, but the
    # production schema includes triggers that fire on INSERT. To
    # exercise T4 against a minimal real schema, use migration 001
    # which sets up memories + FTS + triggers properly.
    migrations_dir = (
        Path(__file__).resolve().parents[2]
        / "src" / "sage_memory" / "migrations"
    )
    # FTS5 + vec0 are virtual tables; the production loader applies
    # them outside a transaction. Skip the vec0 (`memories_vec`) bits
    # by reading 001 and stripping vec0 — we don't need it here.
    # Simpler: use _migrate against the production dir. But it needs
    # sqlite_vec loaded too.
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _migrate(conn, migrations_dir=migrations_dir)

    abs_path = tmp_path / "a.py"
    abs_path.write_text("")
    memory_id = upsert_file_memory(
        conn, abs_path=abs_path, rel_path="a.py", language="py",
    )
    assert isinstance(memory_id, str) and memory_id
    conn.close()
