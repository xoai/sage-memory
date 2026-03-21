"""Database layer — project-aware, dual-database architecture.

sage-memory resolves two databases per request:
  1. Project DB: .sage-memory/memory.db at the active project root
  2. Global DB:  ~/.sage-memory/memory.db for cross-project knowledge

Project root is determined by (in priority order):
  1. Explicit set_project(path) call (recommended)
  2. SAGE_PROJECT_ROOT environment variable
  3. Walk up from cwd looking for markers (.git, pyproject.toml, etc.)
  4. Global DB only (no project detected)

IMPORTANT: Project root is NEVER cached for the session lifetime.
It is re-evaluated on every tool call to handle MCP servers that
stay running across project switches.
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
    """Walk up from start (default: cwd) looking for project markers.

    Stops at home directory or filesystem root. Returns None if no
    markers found. Will NOT return home directory itself even if it
    has markers (e.g. a dotfiles .git).
    """
    current = (start or Path.cwd()).resolve()
    home = Path.home().resolve()

    # Safety: if cwd IS home, don't treat home as project root
    if current == home:
        return None

    while True:
        # Safety: never return home directory as project root
        if current == home:
            return None

        for marker in PROJECT_MARKERS:
            if (current / marker).exists():
                return current

        parent = current.parent
        if parent == current:  # filesystem root
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

# Connections cached by path (safe — same path = same DB file)
_connections: dict[str, sqlite3.Connection] = {}

# Active project root — set explicitly via set_project() or
# override_project_root(). When not set, re-detect per call.
_active_project: Path | None = None
_active_project_set: bool = False  # distinguishes "set to None" from "not set"


def _open(path: Path) -> sqlite3.Connection:
    """Open a connection with pragmas, vec extension, and migrations."""
    key = str(path)
    if key in _connections:
        return _connections[key]

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA cache_size = -2000")

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

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


def _resolve_project_root() -> Path | None:
    """Determine the current project root. Called on every tool call.

    Priority:
      1. Explicit set_project(path) — if called this session
      2. SAGE_PROJECT_ROOT env var — if set (re-read every time)
      3. Walk up from cwd — fallback for simple setups
    """
    if _active_project_set:
        return _active_project

    env_root = os.environ.get("SAGE_PROJECT_ROOT")
    if env_root:
        p = Path(env_root).resolve()
        if p.is_dir():
            return p

    return find_project_root()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Public API — project management
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def set_project(path: str) -> dict:
    """Set the active project root for this session.

    Call once at session start. All subsequent tool calls will use
    this project's database. Creates .sage-memory/ if needed.

    Args:
        path: Absolute or relative path to the project root.

    Returns:
        Dict with project name, database path, and status.
    """
    global _active_project, _active_project_set

    resolved = Path(path).resolve()

    if not resolved.is_dir():
        return {"error": f"Path does not exist or is not a directory: {path}"}

    home = Path.home().resolve()
    if resolved == home:
        return {"error": "Cannot set home directory as project root. Use a project subdirectory."}

    _active_project = resolved
    _active_project_set = True

    # Eagerly create .sage-memory/
    db_path = get_project_db_path(resolved)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    return {
        "project": resolved.name,
        "path": str(resolved),
        "database": str(db_path),
        "status": "active",
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Public API — database access
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def get_project_db() -> sqlite3.Connection | None:
    """Return project-local DB connection, or None if no project detected."""
    root = _resolve_project_root()
    if root is None:
        return None
    return _open(get_project_db_path(root))


def get_global_db() -> sqlite3.Connection:
    """Return the global (~/.sage-memory) DB connection."""
    return _open(get_global_db_path())


def get_db(scope: str = "project") -> sqlite3.Connection:
    """Return the appropriate DB for a scope.

    "project" -> project DB if available, else global
    "global"  -> always global
    """
    if scope == "global":
        return get_global_db()
    project = get_project_db()
    return project if project is not None else get_global_db()


def get_all_dbs() -> list[tuple[str, sqlite3.Connection]]:
    """Return all active DBs for search merging.
    Project DB first (higher priority), then global.
    """
    dbs: list[tuple[str, sqlite3.Connection]] = []
    root = _resolve_project_root()
    if root is not None:
        dbs.append(("project", _open(get_project_db_path(root))))
    dbs.append(("global", get_global_db()))
    return dbs


def get_project_name() -> str | None:
    """Return the active project directory name, or None."""
    root = _resolve_project_root()
    return root.name if root else None


def close_all() -> None:
    """Close all connections and reset project state."""
    global _connections, _active_project, _active_project_set
    for conn in _connections.values():
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except Exception:
            pass
    _connections.clear()
    _active_project = None
    _active_project_set = False


def override_project_root(path: Path | None) -> None:
    """For testing: override the detected project root."""
    global _active_project, _active_project_set
    _active_project = path
    _active_project_set = True
