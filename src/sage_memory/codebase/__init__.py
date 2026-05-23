"""Codebase scan capability — opt-in via the ``[codebase]`` pip extra.

The public entry point is :func:`scan`. All tree-sitter integration is
lazy-imported inside the function bodies so importing this module is
harmless without the extra installed; the first call raises a
``RuntimeError`` with an install hint.

The orchestration that walks files, calls ``extract``, writes
``code_symbols`` and ``codebase_scans`` rows atomically is filled in
during T5 of the build cycle. T1 ships only the lazy-import gate and
the public surface stubs.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ._hash import file_content_hash, memory_content_hash
from ._languages import LANGUAGE_LABELS


_INSTALL_HINT = (
    "scan-codebase requires the [codebase] extra:\n"
    "  pip install 'sage-memory[codebase]'\n"
    "or with uvx, set args to ['sage-memory[codebase]']"
)


class ScanError(Exception):
    """Base class for ``scan()`` errors that map to CLI exit codes.
    Wrappers (CLI, MCP) catch each subclass and translate to the
    documented exit code / envelope.
    """


class ScanLimitExceeded(ScanError):
    """``--limit`` would be exceeded — raised BEFORE any DB writes
    happen. Maps to CLI exit code 3 per spec line 191.
    """

    def __init__(self, file_count: int, limit: int) -> None:
        super().__init__(
            f"--limit {limit} exceeded (would scan {file_count} files). "
            f"Narrow --languages or pass a more specific path."
        )
        self.file_count = file_count
        self.limit = limit


class ScanLockHeld(ScanError):
    """Another scan is already running on this project (rev 3 C2)."""


class ScanRefused(ScanError):
    """``scan()`` refused to run for safety — e.g., scanning the
    user's home directory (rev 3 Minor #1).
    """


@dataclass
class ScanResult:
    files_scanned: int = 0
    files_changed: int = 0
    files_unchanged: int = 0
    files_with_error_nodes: int = 0
    symbols_by_kind: dict[str, int] = field(default_factory=dict)
    relations_imports: int = 0
    relations_calls_resolved: int = 0
    relations_calls_unresolved: int = 0
    parse_errors: int = 0
    elapsed_ms: int = 0
    languages_detected: list[str] = field(default_factory=list)
    project_root: str = ""
    dry_run: bool = False


def upsert_file_memory(
    conn: sqlite3.Connection,
    *,
    abs_path: Path,
    rel_path: str,
    language: str,
) -> str:
    """Idempotently insert a file-memory row, return its ``memory_id``.

    Reads ``abs_path`` from disk, computes the salted hash, looks up an
    existing row by ``content_hash``, returns its id when present.
    Otherwise inserts a new row matching spec §"Each scanned file
    becomes a memory entry":

    - ``title``: ``[file:<lang>] <rel_path>``
    - ``content``: ``Source file (<Language label>)``
    - ``tags``: ``["codebase", "file", "<lang>"]``
    - ``content_hash``: path-salted sha256 (UNIQUE-safe across empty
      ``__init__.py`` siblings)

    The function takes a raw ``sqlite3.Connection`` (NOT the project DB
    wrapper) so callers like T5's ``_scan_file()`` can wrap it in
    ``with conn:`` for atomic per-file transactions. Writes ONLY to the
    ``memories`` table — ``codebase_scans`` / ``code_symbols`` /
    ``code_relations`` / ``scan_locks`` are off-limits here (rev 3 C1).
    """
    data = abs_path.read_bytes()
    content_hash = memory_content_hash(rel_path, data)

    existing = conn.execute(
        "SELECT id FROM memories WHERE content_hash = ?", (content_hash,),
    ).fetchone()
    if existing is not None:
        # sqlite3.Row supports both index and key access; tests may use
        # either fixture style, so prefer index.
        return existing[0]

    memory_id = uuid.uuid4().hex
    title = f"[file:{language}] {rel_path}"
    label = LANGUAGE_LABELS.get(language, language)
    content = f"Source file ({label})"
    tags_json = json.dumps(["codebase", "file", language])
    now = time.time()

    conn.execute(
        """INSERT INTO memories
           (id, title, content, tags, content_hash, embedded,
            created_at, updated_at, accessed_at, access_count)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 0)""",
        (memory_id, title, content, tags_json, content_hash, now, now, now),
    )
    return memory_id


def _scan_file(
    conn: sqlite3.Connection,
    *,
    abs_path: Path,
    rel_path: str,
    language_tag: str,
    query_id: str,
    parser,
    force: bool = False,
) -> tuple[str, str]:
    """Atomically scan one file: extract symbols + register in
    ``codebase_scans``. Returns ``("scanned"|"unchanged", memory_id)``.

    Fast path: if the file's unsalted hash matches the stored
    ``codebase_scans`` entry and ``force`` is false, skip parsing
    entirely.

    Slow path (inside ``with conn:`` so sqlite3 manages BEGIN/COMMIT
    and auto-rollbacks on exception):

    1. ``upsert_file_memory`` (T4) — write to ``memories`` only.
    2. ``DELETE FROM code_symbols WHERE file_memory_id = ?`` — drops
       stale symbols (and ``code_relations`` whose source FK CASCADEs).
    3. ``extract`` (T5) — parse + walk tree.
    4. INSERT each extracted symbol into ``code_symbols``.
    5. ``INSERT OR REPLACE INTO codebase_scans`` — this function is
       the SOLE writer to that table (rev 3 C1: makes orphan-state
       poisoning between memory upsert and symbol insert impossible).

    Order in step 5 is critical: if extract raises after step 1, the
    ``with conn:`` block rolls back the upsert too, so the half-state
    "memory exists but no codebase_scans entry pointing at it" cannot
    persist.
    """
    from ._extract import extract

    file_bytes = abs_path.read_bytes()
    content_hash = file_content_hash(file_bytes)

    existing = conn.execute(
        "SELECT content_hash, file_memory_id FROM codebase_scans "
        "WHERE file_path = ?",
        (str(abs_path),),
    ).fetchone()
    if existing is not None and existing[0] == content_hash and not force:
        return "unchanged", existing[1]

    with conn:
        memory_id = upsert_file_memory(
            conn,
            abs_path=abs_path,
            rel_path=rel_path,
            language=language_tag,
        )
        conn.execute(
            "DELETE FROM code_symbols WHERE file_memory_id = ?",
            (memory_id,),
        )
        extracted = extract(parser, query_id, file_bytes, rel_path)
        _insert_symbols(conn, memory_id, language_tag, extracted.symbols)
        conn.execute(
            "INSERT OR REPLACE INTO codebase_scans "
            "(file_path, content_hash, file_memory_id, language, last_scanned) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(abs_path), content_hash, memory_id, language_tag, time.time()),
        )

    return "scanned", memory_id


def _insert_symbols(
    conn: sqlite3.Connection,
    file_memory_id: str,
    language_tag: str,
    symbols,
) -> None:
    """INSERT every ExtractedSymbol into ``code_symbols``. Symbols
    arrive sorted parent-first (see ``_process_python``); inserting in
    that order satisfies the self-referential FK on ``parent_id`` under
    the default ``PRAGMA foreign_keys = ON`` (immediate enforcement).
    """
    now = time.time()
    for s in symbols:
        conn.execute(
            """INSERT INTO code_symbols
               (id, file_memory_id, name, qualified_name, kind, language,
                signature, line_start, line_end, parent_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                s.id, file_memory_id, s.name, s.qualified_name, s.kind,
                language_tag, s.signature, s.line_start, s.line_end,
                s.parent_id, now,
            ),
        )


def _require_extra() -> None:
    """Lazy-import gate. Raises RuntimeError with install hint when the
    ``[codebase]`` extra is not installed.
    """
    try:
        import tree_sitter_language_pack  # noqa: F401
    except ImportError:
        raise RuntimeError(_INSTALL_HINT) from None


def scan(
    root: Path | str | None = None,
    *,
    languages: list[str] | None = None,
    include_ignored: bool = False,
    limit: int = 5000,
    force: bool = False,
    dry_run: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> ScanResult:
    """Run a codebase scan over ``root`` (defaults to the active
    project root resolved via ``db._resolve_project_root``).

    Lazy-imports tree-sitter; raises ``RuntimeError`` with an install
    hint if the ``[codebase]`` extra is not installed.

    Pipeline:
      1. Walk files matching known extensions (T3 ``_walker.walk``).
      2. For each file, ``_scan_file`` parses + writes
         ``code_symbols`` + ``codebase_scans`` atomically (T5).
      3. After all files are scanned, ``resolve_codebase`` writes
         ``code_relations`` rows with per-language target resolution
         (T9a/T9b).
      4. Aggregate counts for the summary surface (T10a).

    Safety features (T10b): refuses to scan ``$HOME`` (rev 3 Minor #1),
    enforces ``--limit`` pre-walk before any writes (rev 2 M3), and
    holds a project-scoped advisory lock for the duration of the
    scan via ``scan_locks`` (rev 3 C2). The lock is released in a
    finally-block so a crashed scan doesn't permanently lock the
    project; a 600s stale-row recovery in ``_lock`` is the backstop.
    """
    import time as _time

    _require_extra()

    from ..db import get_db, _resolve_project_root
    from ._walker import walk

    start = _time.time()

    if root is None:
        resolved = _resolve_project_root()
        if resolved is None:
            raise RuntimeError(
                "scan-codebase: could not resolve a project root. "
                "Pass `root=` explicitly or run from inside a project."
            )
        root_path = resolved
    else:
        root_path = Path(root)
    root_path = root_path.resolve()

    # Rev 3 Minor #1: refuse to scan the user's home directory. The
    # cost-benefit of a 5000-file scan over $HOME is uniformly bad —
    # at best it's a confused user, at worst it's writing tens of
    # thousands of code_symbols rows for every dotfile in their home.
    if root_path == Path.home().resolve():
        raise ScanRefused(
            f"Refusing to scan home directory ({root_path}). "
            f"Pass a more specific path."
        )

    files = list(
        walk(root_path, languages=languages, include_ignored=include_ignored)
    )

    result = ScanResult(
        project_root=str(root_path),
        languages_detected=sorted({lt for _, lt, _, _ in files}),
        dry_run=dry_run,
    )

    if dry_run:
        # Dry-run skips the limit check + lock acquire — neither
        # matters because nothing is written. Users can dry-run
        # against a huge tree to estimate scan time / files count.
        result.files_scanned = len(files)
        result.elapsed_ms = int((_time.time() - start) * 1000)
        return result

    # Rev 2 M3 fix + rev 3 lock-in: pre-walk limit check happens
    # BEFORE we open the DB or take the lock so a too-big scan
    # leaves zero side effects. The CLI gets exit 3 and the user
    # can narrow scope before re-running.
    if len(files) > limit:
        raise ScanLimitExceeded(file_count=len(files), limit=limit)

    conn = get_db("project")

    from ._lock import acquire_scan_lock, release_scan_lock

    if not acquire_scan_lock(conn):
        raise ScanLockHeld(
            "scan already in progress on this project (another sage-memory "
            "scan-codebase is running, or a prior scan crashed within the "
            "last 10 minutes)."
        )

    try:
        return _do_scan_after_lock(
            conn, root_path, files, result, force, progress, start,
        )
    finally:
        # MUST release even when _do_scan_after_lock raises so a
        # crashed scan doesn't permanently lock the project. The
        # 600s stale-row recovery is a backstop, NOT the primary
        # release mechanism.
        release_scan_lock(conn)


def _do_scan_after_lock(
    conn, root_path, files, result, force, progress, start,
):
    """The actual per-file scan loop + resolve, factored out so the
    surrounding lock acquire/release in ``scan()`` is a clean
    try/finally pair.
    """
    import time as _time

    from tree_sitter_language_pack import get_parser
    from ._languages import EXT_MAP
    from ._resolve import resolve_codebase

    parsers_by_grammar: dict[str, object] = {}

    def _get_cached_parser(grammar_name: str) -> object:
        if grammar_name not in parsers_by_grammar:
            parsers_by_grammar[grammar_name] = get_parser(grammar_name)
        return parsers_by_grammar[grammar_name]

    total = len(files)
    # T11 Major #1 fix: collect every file_memory_id touched this run
    # so the summary aggregation reflects ONLY this scan, not the
    # entire project DB. A previous scan of a different path
    # shouldn't inflate the current run's symbol/relation counts.
    touched_memory_ids: set[str] = set()
    for i, (rel, language_tag, grammar_name, query_id) in enumerate(files):
        abs_path = root_path / rel
        try:
            parser = _get_cached_parser(grammar_name)
            status, memory_id = _scan_file(
                conn,
                abs_path=abs_path,
                rel_path=str(rel),
                language_tag=language_tag,
                query_id=query_id,
                parser=parser,
                force=force,
            )
            touched_memory_ids.add(memory_id)
            if status == "scanned":
                result.files_changed += 1
            else:  # "unchanged"
                result.files_unchanged += 1
            result.files_scanned += 1
        except Exception:
            # Catastrophic per-file failure (binary file, grammar
            # load failure, etc.) — counted, scan continues. The
            # over-broad except is intentional resilience; the
            # parse_errors counter surfaces the count in the summary.
            result.parse_errors += 1
        if progress is not None:
            progress(i + 1, total)
    conn.commit()

    # Build parsers dict accepting either language_tag or grammar_name
    # — _resolve.resolve_codebase tries both keys in order.
    parsers_for_resolve: dict[str, object] = dict(parsers_by_grammar)
    for ext, (lt, gn, _qid) in EXT_MAP.items():
        if gn in parsers_by_grammar:
            parsers_for_resolve.setdefault(lt, parsers_by_grammar[gn])

    resolve_result = resolve_codebase(
        conn,
        project_root=root_path,
        parsers=parsers_for_resolve,
    )
    result.files_with_error_nodes = resolve_result.files_with_error_nodes
    result.parse_errors += resolve_result.parse_errors
    conn.commit()

    # Aggregate symbols + relations from the DB rather than carrying
    # per-task counters everywhere — keeps T5/T9 contracts unchanged
    # and survives idempotent re-scans (the final DB state is what
    # users see). T11 Major #1 fix: scope the aggregation to the
    # `file_memory_id`s touched THIS run so a partial-scope rescan
    # doesn't report cumulative project-wide totals.
    result.symbols_by_kind = _aggregate_symbol_counts(conn, touched_memory_ids)
    (
        result.relations_imports,
        result.relations_calls_resolved,
        result.relations_calls_unresolved,
    ) = _aggregate_relation_counts(conn, touched_memory_ids)

    result.elapsed_ms = int((_time.time() - start) * 1000)
    return result


def _aggregate_symbol_counts(
    conn: sqlite3.Connection, file_memory_ids: set[str],
) -> dict[str, int]:
    """Symbol-kind counts for files touched in THIS scan only.

    Empty ``file_memory_ids`` (e.g. all-unchanged-no-write scan)
    returns an empty dict — sqlite chokes on ``IN ()`` so we guard.
    Synthetic ``MODULE`` anchor symbols are excluded; they're an
    internal artifact agents shouldn't see.
    """
    if not file_memory_ids:
        return {}
    placeholders = ",".join("?" * len(file_memory_ids))
    out: dict[str, int] = {}
    for row in conn.execute(
        f"SELECT kind, COUNT(*) AS n FROM code_symbols "
        f"WHERE kind != 'MODULE' "
        f"AND file_memory_id IN ({placeholders}) "
        f"GROUP BY kind ORDER BY kind",
        tuple(file_memory_ids),
    ):
        out[row["kind"]] = row["n"]
    return out


def _aggregate_relation_counts(
    conn: sqlite3.Connection, file_memory_ids: set[str],
) -> tuple[int, int, int]:
    """Relation counts for source symbols in files touched THIS scan.

    Filtering on the SOURCE's file_memory_id (not the target's) is
    the right scope: a relation "belongs to" the file whose code
    issued the call/import, even when its target is cross-file.
    """
    if not file_memory_ids:
        return 0, 0, 0
    placeholders = ",".join("?" * len(file_memory_ids))
    imports = 0
    calls_resolved = 0
    calls_unresolved = 0
    for row in conn.execute(
        f"SELECT cr.kind, "
        f"  SUM(CASE WHEN cr.target_symbol_id IS NOT NULL THEN 1 ELSE 0 END) AS resolved, "
        f"  SUM(CASE WHEN cr.target_symbol_id IS NULL THEN 1 ELSE 0 END) AS unresolved "
        f"FROM code_relations cr "
        f"JOIN code_symbols cs ON cs.id = cr.source_symbol_id "
        f"WHERE cs.file_memory_id IN ({placeholders}) "
        f"GROUP BY cr.kind",
        tuple(file_memory_ids),
    ):
        if row["kind"] == "imports":
            imports += (row["resolved"] or 0) + (row["unresolved"] or 0)
        elif row["kind"] == "calls":
            calls_resolved += row["resolved"] or 0
            calls_unresolved += row["unresolved"] or 0
    return imports, calls_resolved, calls_unresolved
