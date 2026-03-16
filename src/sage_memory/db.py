"""Database layer — project-aware, dual-database architecture.

On startup, sage-memory resolves two databases:
  1. Project DB: .sage-memory/memory.db at the nearest project root
  2. Global DB:  ~/.sage-memory/memory.db for cross-project knowledge

Project root is detected by walking up from cwd looking for markers
(.git, pyproject.toml, package.json, Cargo.toml, go.mod, etc.).
If no project root is found, only the global DB is used.

Search queries hit both databases and merge results (project-first).
Store operations target one database based on the 'scope' parameter.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import sqlite_vec

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Project detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PROJECT_MARKERS = (
    ".git", "pyproject.toml", "package.json", "Cargo.toml",
    "go.mod", "pom.xml", "build.gradle", "Makefile",
    "requirements.txt", "setup.py", "composer.json",
)

SAGE_DIR = ".sage-memory"
DB_NAME = "memory.db"


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from start (default: cwd) looking for project markers."""
    current = (start or Path.cwd()).resolve()
    home = Path.home().resolve()

    while True:
        for marker in PROJECT_MARKERS:
            if (current / marker).exists():
                return current

        parent = current.parent
        # Stop at home dir or filesystem root — don't go above home
        if parent == current or current == home:
            return None
        current = parent


def get_global_db_path() -> Path:
    return Path.home() / SAGE_DIR / DB_NAME


def get_project_db_path(project_root: Path) -> Path:
    return project_root / SAGE_DIR / DB_NAME


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Connection management
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Cached connections
_connections: dict[str, sqlite3.Connection] = {}
_project_root: Path | None = None
_resolved = False


def _open(path: Path) -> sqlite3.Connection:
    """Open a connection with pragmas, vec extension, and migrations."""
    key = str(path)
    if key in _connections:
        return _connections[key]

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Pragmas
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA cache_size = -2000")

    # Load sqlite-vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    # Run migrations
    _migrate(conn)

    _connections[key] = conn
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for sql_file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        version = int(sql_file.stem.split("_")[0])
        if version <= current:
            continue
        conn.executescript(sql_file.read_text("utf-8"))
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()


def _resolve() -> None:
    """Detect project root once on first access."""
    global _project_root, _resolved
    if _resolved:
        return
    _project_root = find_project_root()
    _resolved = True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def get_project_db() -> sqlite3.Connection | None:
    """Return project-local DB connection, or None if no project detected."""
    _resolve()
    if _project_root is None:
        return None
    return _open(get_project_db_path(_project_root))


def get_global_db() -> sqlite3.Connection:
    """Return the global (~/.sage-memory) DB connection."""
    return _open(get_global_db_path())


def get_db(scope: str = "project") -> sqlite3.Connection:
    """Return the appropriate DB for a scope.

    "project" → project DB if available, else global
    "global"  → always global
    """
    if scope == "global":
        return get_global_db()

    project = get_project_db()
    return project if project is not None else get_global_db()


def get_all_dbs() -> list[tuple[str, sqlite3.Connection]]:
    """Return all active DBs for search merging: [(label, conn), ...]
    Project DB first (higher priority), then global.
    """
    _resolve()
    dbs: list[tuple[str, sqlite3.Connection]] = []

    if _project_root is not None:
        dbs.append(("project", _open(get_project_db_path(_project_root))))

    dbs.append(("global", get_global_db()))
    return dbs


def get_project_name() -> str | None:
    """Return the detected project directory name, or None."""
    _resolve()
    return _project_root.name if _project_root else None


def close_all() -> None:
    global _connections, _resolved
    for conn in _connections.values():
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except Exception:
            pass
    _connections.clear()
    _resolved = False


def override_project_root(path: Path | None) -> None:
    """For testing: override the detected project root."""
    global _project_root, _resolved
    _project_root = path
    _resolved = True
