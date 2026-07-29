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
import re
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


# ─── P2-3 (SM-QUAL-01) — SQL identifier whitelisting ──────────────
#
# Hygiene guard for the few places SQL is built with f-string
# IDENTIFIERS (cli_reindex backup tables, cli_dedup PRAGMA). Values
# there are internal constants — no injection is currently reachable
# — but a future edit passing an unsafe value now fails loudly here
# instead of silently executing it. Parameters still always use `?`
# binding; this guard is ONLY for identifiers, which cannot be bound.

_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def require_sql_identifier(name: str) -> str:
    """Return `name` unchanged iff it is a safe SQL identifier
    (strict `^[A-Za-z_][A-Za-z0-9_]*$`); raise ValueError otherwise."""
    if not _SQL_IDENT_RE.match(name):
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return name


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


# ─── P0-3 (SM-SEC-03) — set_project scoping ───────────────────────
#
# An unauthenticated network transport + arbitrary set_project was a
# remote file-read/write path: point the server at any directory and
# it creates .sage-memory/ there and reads content back through
# search. The allowlist below scopes set_project to the operator's
# launch context plus explicit extras.

# Always-denied directories, even when under an allowed root.
def _sensitive_dirs() -> tuple[Path, ...]:
    home = Path.home()
    return (
        home / ".ssh",
        home / ".gnupg",
        home / ".aws",
        Path("/etc"),
    )


def _allowed_project_roots() -> list[Path]:
    """Allowed set_project roots (subtree-inclusive).

    Default: the process's detected project root — SAGE_PROJECT_ROOT
    when valid, else the marker walk-up from cwd. When no markers
    exist (plain folder), the cwd itself is the launch context: an
    operator starting the server in a markerless project must still be
    able to point at it (zero-config, invariant 9). Plus every path in
    SAGE_ALLOWED_ROOTS (os.pathsep-separated).
    """
    roots: list[Path] = []
    env_root = os.environ.get("SAGE_PROJECT_ROOT")
    detected: Path | None = None
    if env_root:
        p = Path(env_root).resolve()
        if p.is_dir():
            detected = p
    if detected is None:
        detected = find_project_root()
    roots.append(detected if detected is not None else Path.cwd().resolve())

    extra = os.environ.get("SAGE_ALLOWED_ROOTS")
    if extra:
        for part in extra.split(os.pathsep):
            part = part.strip()
            if part:
                roots.append(Path(part).resolve())
    return roots


def _project_scope_error(resolved: Path) -> str | None:
    """Return an error message when `resolved` may not be a project
    root, else None. Denylist wins over allowlist.

    Containment uses Path.is_relative_to on RESOLVED paths — never
    str.startswith, which wrongly admits sibling-prefix paths
    (/tmp/proj-secret vs /tmp/proj).
    """
    for denied in _sensitive_dirs():
        try:
            if resolved.is_relative_to(denied):
                return (
                    f"Refusing sensitive directory as project root: "
                    f"{resolved}"
                )
        except ValueError:
            continue
    for root in _allowed_project_roots():
        try:
            if resolved.is_relative_to(root):
                return None
        except ValueError:
            continue
    return (
        f"Project root outside allowed roots: {resolved}. Allowed: "
        f"the detected project root (or launch directory) subtree, "
        f"plus SAGE_ALLOWED_ROOTS (os.pathsep-separated)."
    )


def get_db_path(conn: sqlite3.Connection) -> Path:
    """Resolve which DB file a live ``sqlite3.Connection`` is bound to.

    Implements the M3.0 helper that ADR-009's per-DB ownership check
    (M3.2) depends on. Uses SQLite's ``PRAGMA database_list`` to read
    the path that this exact connection was opened against — works
    regardless of whether the connection went through ``_open()``'s
    cache or was constructed directly (e.g., the hub.search read-only
    URI form).

    Returns the resolved absolute path. Raises ``RuntimeError`` if
    the connection is closed or pointing at an in-memory DB.
    """
    rows = conn.execute("PRAGMA database_list").fetchall()
    if not rows:
        raise RuntimeError("connection has no main database")
    # PRAGMA database_list returns (seq, name, file); main is seq=0.
    main = rows[0]
    file_path = main[2] if not isinstance(main, sqlite3.Row) else main["file"]
    if not file_path:
        raise RuntimeError("connection has no on-disk database (in-memory?)")
    return Path(file_path).resolve()


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


def _strip_strings_and_comments(s: str) -> str:
    """Mask string literals and SQL comments with same-length whitespace,
    so position-based regex/keyword scans don't false-match on content
    inside them. Preserves character offsets so callers can slice the
    original by index.
    """
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        nxt = s[i + 1] if i + 1 < n else ""
        # Line comment: -- to end of line
        if c == "-" and nxt == "-":
            j = s.find("\n", i)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
            continue
        # Block comment: /* ... */
        if c == "/" and nxt == "*":
            j = s.find("*/", i + 2)
            if j == -1:
                j = n
            else:
                j += 2
            out.append(" " * (j - i))
            i = j
            continue
        # Single-quoted string (with '' escape)
        if c == "'":
            j = i + 1
            while j < n:
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":
                        j += 2  # escaped quote inside string
                        continue
                    j += 1
                    break
                j += 1
            else:
                j = n
            out.append(" " * (j - i))
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


_BEGIN_KW = re.compile(r"\bBEGIN\b", re.IGNORECASE)
_END_KW = re.compile(r"\bEND\b", re.IGNORECASE)
_CREATE_VIRTUAL_RE = re.compile(
    r"^\s*CREATE\s+VIRTUAL\s+TABLE\b", re.IGNORECASE
)


def _split_statements(script: str) -> list[str]:
    """Split a SQL script into individual top-level statements.

    Respects: single-quoted strings (with '' escape), -- line comments,
    /* */ block comments, and CREATE TRIGGER ... BEGIN ... END; blocks
    (so semicolons inside trigger bodies don't split the trigger).

    Returns non-empty statement strings (trailing whitespace stripped,
    but trailing semicolon preserved for clarity).
    """
    statements: list[str] = []
    buffer: list[str] = []
    for line in script.splitlines(keepends=True):
        buffer.append(line)
        joined = "".join(buffer)
        masked = _strip_strings_and_comments(joined)
        # Inside a trigger body if BEGIN count > END count.
        begin_count = len(_BEGIN_KW.findall(masked))
        end_count = len(_END_KW.findall(masked))
        if begin_count > end_count:
            continue
        if sqlite3.complete_statement(joined):
            stmt = joined.strip()
            if stmt and stmt.rstrip(";").strip():
                statements.append(stmt)
            buffer = []
    # Trailing content without a terminating semicolon
    if buffer:
        rest = "".join(buffer).strip()
        if rest and rest.rstrip(";").strip():
            statements.append(rest)
    return statements


def _is_create_virtual(stmt: str) -> bool:
    """True if the statement is a CREATE VIRTUAL TABLE (FTS5/vec0 etc)."""
    masked = _strip_strings_and_comments(stmt)
    return _CREATE_VIRTUAL_RE.match(masked) is not None


def _migrate(
    conn: sqlite3.Connection,
    migrations_dir: Path | None = None,
) -> None:
    """Apply pending migrations in two phases per file:

    PHASE A — virtual-table CREATEs (FTS5, vec0). Run outside any
    transaction because some SQLite versions reject CREATE VIRTUAL TABLE
    inside a BEGIN block. Idempotent via `IF NOT EXISTS`.

    PHASE B — non-virtual DDL + DML + `PRAGMA user_version = N`. Wrapped
    in BEGIN/COMMIT; on exception, ROLLBACK reverts the file's PHASE B
    work atomically. user_version only advances on full success.

    See .sage/docs/decision-001-storage-schema.md §Migration Runner Contract.

    Args:
        conn: sqlite3 connection (must have foreign_keys = ON).
        migrations_dir: directory holding `NNN_*.sql` files. Defaults to
            the production migrations directory. Tests pass tmpdirs.
    """
    if migrations_dir is None:
        migrations_dir = _MIGRATIONS_DIR

    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        version = int(sql_file.stem.split("_")[0])
        if version <= current:
            continue

        script = sql_file.read_text("utf-8")
        statements = _split_statements(script)
        virtual_stmts = [s for s in statements if _is_create_virtual(s)]
        other_stmts = [s for s in statements if not _is_create_virtual(s)]

        # PHASE A: virtual tables, outside txn (idempotent via IF NOT EXISTS).
        for stmt in virtual_stmts:
            conn.execute(stmt)

        # PHASE B: non-virtual DDL + user_version bump, atomic.
        try:
            conn.execute("BEGIN")
            for stmt in other_stmts:
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


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

    # P0-3 (SM-SEC-03): scope to the launch context + SAGE_ALLOWED_ROOTS.
    scope_error = _project_scope_error(resolved)
    if scope_error is not None:
        return {"error": scope_error}

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
