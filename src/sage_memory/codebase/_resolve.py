"""Two-pass call-graph resolution.

Pass 1 (``_build_definitions``) reads ``code_symbols`` and
``codebase_scans`` to build a lookup table mapping symbol identities
to their database rows.

Pass 2 (``resolve_codebase``) re-walks every scanned file, re-extracts
relations via the language's processor, and writes a ``code_relations``
row per relation. The row's ``target_symbol_id`` is filled when the
language-specific resolver can map the relation's ``target_name`` to a
symbol in the index; otherwise it's NULL and ``confidence='unresolved'``
— per spec §"Resolution semantics", every call site is recorded so
unresolved relations remain visible to agents (they don't silently
disappear from search).

Module-level imports are anchored to a per-file synthetic
``<module>`` symbol (kind=``MODULE``, line_start=1) created lazily on
first use. The synthetic symbol exists so ``code_relations``'s
``source_symbol_id NOT NULL`` constraint can be satisfied for the
common ``import X`` / ``from X import Y`` shape. Module-level CALLS
(rare in real code) are still skipped — the cost of recording every
``print()`` at the top of every script outweighs the search value.

T9a ships the orchestration plus the Python resolver. T9b extends
``_RESOLVERS`` with the other eight languages.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ._extract import ExtractedRelation, extract
from ._languages import EXT_MAP


# T11 Major #2 fix: derive (grammar_name, query_id) deterministically
# from the stored ``language`` column in codebase_scans — NOT from the
# filesystem. The filesystem may have changed between scan and resolve
# (a new .cpp file appearing next to a previously-scanned .h would flip
# the .h heuristic), which would route extract() to the wrong grammar.
# Each language has exactly one canonical (grammar, query_id) pair,
# computed once at module load.
_LANGUAGE_TO_GRAMMAR_QUERY: dict[str, tuple[str, str]] = {}
for _ext, (_lang_tag, _grammar, _qid) in EXT_MAP.items():
    # First-write wins; for languages with multiple extensions
    # (e.g. js: .js / .mjs / .cjs) the canonical grammar+query is
    # identical so the choice doesn't matter.
    _LANGUAGE_TO_GRAMMAR_QUERY.setdefault(_lang_tag, (_grammar, _qid))
# `.h` resolves to language="c" or "cpp" at walk time; both are
# already covered by EXT_MAP entries for `.c` / `.cpp`.


@dataclass
class ResolveResult:
    relations_resolved: int = 0
    relations_unresolved: int = 0
    files_walked: int = 0
    files_skipped: int = 0  # disk-missing or extract error
    parse_errors: int = 0
    files_with_error_nodes: int = 0


@dataclass
class DefinitionsTable:
    """Snapshot of ``code_symbols`` + ``codebase_scans`` keyed for
    cross-file resolution.

    Attributes
    ----------
    per_file_symbols : ``dict[file_memory_id, dict[qualified_name → symbol_id]]``
        Used for same-file resolution and for parent-class lookup in
        ``self.X`` style calls.
    file_by_module : ``dict[(language, module_path), file_memory_id]``
        Used for cross-file import / call resolution. ``module_path``
        is the rel_path with the language-specific extension stripped
        and directory separators turned into dots (Python convention;
        other languages override in T9b).
    rel_path_by_memory : ``dict[file_memory_id, str]``
        Used to identify a relation's source file and translate
        relative imports.
    memory_by_rel_path : ``dict[str, file_memory_id]``
        P1-1: precomputed reverse of ``rel_path_by_memory`` — the TS/Rust
        resolvers previously rebuilt it per call (O(files) per relation).
    dir_to_memory_ids : ``dict[str, list[file_memory_id]]``
        P1-1: precomputed directory → files map for the Go/Java
        sibling lookups (same reason).
    language_by_memory : ``dict[file_memory_id, str]``
        P1-1: file language lookup for the DB-driven re-resolve pass.
    """

    per_file_symbols: dict[str, dict[str, str]] = field(default_factory=dict)
    file_by_module: dict[tuple[str, str], str] = field(default_factory=dict)
    rel_path_by_memory: dict[str, str] = field(default_factory=dict)
    memory_by_rel_path: dict[str, str] = field(default_factory=dict)
    dir_to_memory_ids: dict[str, list[str]] = field(default_factory=dict)
    language_by_memory: dict[str, str] = field(default_factory=dict)


def resolve_codebase(
    conn: sqlite3.Connection,
    *,
    project_root: Path,
    parsers: dict[str, object],
    extracted: dict[str, dict] | None = None,
) -> ResolveResult:
    """Pass 1 + Pass 2.

    Two modes (P1-1):

    ``extracted=None`` (legacy / ``--full-resolve`` escape hatch):
    walks every file in ``codebase_scans``, re-reads + re-extracts
    from disk, resolves, inserts. This is the pre-P1-1 path — kept
    verbatim for direct callers and debugging.

    ``extracted={memory_id: {"relations": [...], "had_error_nodes":
    bool}}`` (incremental, the default from ``scan()``): relations
    for files re-parsed THIS run arrive pre-extracted from the scan
    pass — no disk reads, no re-parsing. Everything else is resolved
    from the DB:

      1. INSERT relations for the extracted files (their old rows
         were CASCADE-deleted with their symbols).
      2. Restore ``_resolve_orphans`` — rows in unchanged files whose
         target symbols lived in a CHANGED file (snapshotted by
         ``_scan_file`` before the cascade) — as unresolved.
      3. DB-driven re-resolve: every still-unresolved row is re-run
         through its language resolver against the fresh definitions
         table and UPDATEd in place when a target is found. This is
         the cross-file dependent set: an unchanged file's relation
         resolves when a newly-scanned file defines the target.

    ``parsers`` maps language_tag (e.g. ``"py"``) or grammar_name
    (e.g. ``"python"``) to a tree-sitter Parser. Only used by the
    legacy disk path.
    """
    project_root = Path(project_root).resolve()
    defs = _build_definitions(conn, project_root)

    if extracted is None:
        return _resolve_from_disk(
            conn, project_root=project_root, parsers=parsers, defs=defs,
        )
    return _resolve_incremental(conn, defs=defs, extracted=extracted)


def _resolve_from_disk(
    conn: sqlite3.Connection,
    *,
    project_root: Path,
    parsers: dict[str, object],
    defs: DefinitionsTable,
) -> ResolveResult:
    """Pre-P1-1 full re-parse path (escape hatch)."""

    result = ResolveResult()
    scans = conn.execute(
        "SELECT file_path, file_memory_id, language FROM codebase_scans"
    ).fetchall()

    for scan in scans:
        abs_path = Path(scan["file_path"])
        memory_id = scan["file_memory_id"]
        language = scan["language"]

        if not abs_path.exists():
            result.files_skipped += 1
            continue

        # T11 Major #2 fix: trust the stored ``language`` column over
        # the current filesystem state. Avoids the .h heuristic
        # potentially flipping between scan and resolve.
        gq = _LANGUAGE_TO_GRAMMAR_QUERY.get(language)
        if gq is None:
            result.files_skipped += 1
            continue
        grammar_name, query_id = gq

        # T11 Major #3 fix: lazily create the parser if not pre-loaded.
        # Previously a file scanned in a prior run whose grammar wasn't
        # in `parsers` (e.g. `--languages py` rescan after a mixed
        # scan) was silently skipped, producing zero relations and no
        # diagnostic.
        parser = parsers.get(language) or parsers.get(grammar_name)
        if parser is None:
            try:
                from tree_sitter_language_pack import get_parser
                parser = get_parser(grammar_name)
                parsers[language] = parser
                parsers[grammar_name] = parser
            except Exception:
                result.files_skipped += 1
                continue

        try:
            rel_path = str(abs_path.relative_to(project_root))
        except ValueError:
            result.files_skipped += 1
            continue

        try:
            source_bytes = abs_path.read_bytes()
            extracted = extract(parser, query_id, source_bytes, rel_path)
        except Exception:
            result.parse_errors += 1
            continue

        result.files_walked += 1
        if extracted.had_error_nodes:
            # Parser recovered — symbols/relations from the well-formed
            # parts are usable, so we still resolve them. Just count.
            result.files_with_error_nodes += 1

        per_file_syms = defs.per_file_symbols.get(memory_id, {})
        resolver = _RESOLVERS.get(language)
        module_anchor_id: str | None = None  # lazily created

        now = time.time()
        for ext_rel in extracted.relations:
            source_id = per_file_syms.get(ext_rel.source_qname)
            if source_id is None:
                if ext_rel.source_qname == "" and ext_rel.kind == "imports":
                    # Module-level imports get the synthetic <module>
                    # anchor so the row can satisfy source_symbol_id
                    # NOT NULL. Calls at module level are rare and
                    # intentionally skipped — see module docstring.
                    if module_anchor_id is None:
                        module_anchor_id = _get_or_create_module_anchor(
                            conn, memory_id, language,
                        )
                        # Register on defs so any subsequent imports
                        # in this file hit the same row without
                        # re-querying.
                        per_file_syms["<module>"] = module_anchor_id
                        defs.per_file_symbols[memory_id] = per_file_syms
                    source_id = module_anchor_id
                else:
                    continue

            target_id = None
            if resolver is not None:
                target_id = resolver(
                    ext_rel, language, memory_id, defs,
                )
            confidence = "resolved" if target_id is not None else "unresolved"

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO code_relations
                       (id, source_symbol_id, target_symbol_id, target_name,
                        kind, confidence, line, column_start, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        uuid.uuid4().hex, source_id, target_id,
                        ext_rel.target_name, ext_rel.kind, confidence,
                        ext_rel.line, ext_rel.column_start, now,
                    ),
                )
                if target_id is not None:
                    result.relations_resolved += 1
                else:
                    result.relations_unresolved += 1
            except sqlite3.IntegrityError:
                # FK violation (target_symbol_id pointed at a deleted
                # row) or other constraint failure — count as skipped.
                pass

    return result


# ───────────────────────────────────────────────────────────────────
# P1-1 — Incremental resolve (DB-driven, no re-parsing)
# ───────────────────────────────────────────────────────────────────


def _resolve_incremental(
    conn: sqlite3.Connection,
    *,
    defs: DefinitionsTable,
    extracted: dict[str, dict],
) -> ResolveResult:
    result = ResolveResult()

    # ── 1. Insert relations for files re-parsed this run ──────────
    for memory_id, sink in extracted.items():
        language = defs.language_by_memory.get(memory_id)
        resolver = _RESOLVERS.get(language) if language else None
        per_file_syms = defs.per_file_symbols.get(memory_id, {})
        module_anchor_id: str | None = None

        if sink.get("had_error_nodes"):
            result.files_with_error_nodes += 1
        result.files_walked += 1
        now = time.time()
        for ext_rel in sink.get("relations", []):
            source_id = per_file_syms.get(ext_rel.source_qname)
            if source_id is None:
                if ext_rel.source_qname == "" and ext_rel.kind == "imports":
                    if module_anchor_id is None:
                        module_anchor_id = _get_or_create_module_anchor(
                            conn, memory_id, language,
                        )
                        per_file_syms["<module>"] = module_anchor_id
                        defs.per_file_symbols[memory_id] = per_file_syms
                    source_id = module_anchor_id
                else:
                    continue

            target_id = None
            if resolver is not None:
                target_id = resolver(ext_rel, language, memory_id, defs)
            confidence = "resolved" if target_id is not None else "unresolved"

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO code_relations
                       (id, source_symbol_id, target_symbol_id, target_name,
                        kind, confidence, line, column_start, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        uuid.uuid4().hex, source_id, target_id,
                        ext_rel.target_name, ext_rel.kind, confidence,
                        ext_rel.line, ext_rel.column_start, now,
                    ),
                )
                if target_id is not None:
                    result.relations_resolved += 1
                else:
                    result.relations_unresolved += 1
            except sqlite3.IntegrityError:
                pass

    # ── 2. Restore CASCADE orphans as unresolved ──────────────────
    # Rows in unchanged files whose target symbols lived in a changed
    # file; snapshotted by _scan_file before the delete cascade.
    orphan_table = conn.execute(
        "SELECT name FROM sqlite_temp_master WHERE type='table' "
        "AND name='_resolve_orphans'"
    ).fetchone()
    if orphan_table is not None:
        conn.execute(
            """INSERT OR IGNORE INTO code_relations
               (id, source_symbol_id, target_symbol_id, target_name,
                kind, confidence, line, column_start, created_at)
               SELECT id, source_symbol_id, NULL, target_name, kind,
                      'unresolved', line, column_start, ?
               FROM _resolve_orphans""",
            (time.time(),),
        )
        conn.execute("DELETE FROM _resolve_orphans")

    # ── 3. DB-driven re-resolve of all unresolved rows ────────────
    # The cross-file dependent set, computed in memory instead of by
    # query: every unresolved row gets another pass through its
    # language resolver against the fresh definitions table. Cheap
    # (dict lookups) — the pre-P1-1 cost was PARSING, not resolving.
    unresolved = conn.execute(
        """SELECT cr.id, cr.kind, cr.target_name, cr.line,
                  cr.column_start, cs.qualified_name AS source_qname,
                  cs.language AS language, cs.file_memory_id AS memory_id
           FROM code_relations cr
           JOIN code_symbols cs ON cs.id = cr.source_symbol_id
           WHERE cr.target_symbol_id IS NULL"""
    ).fetchall()
    for row in unresolved:
        resolver = _RESOLVERS.get(row["language"])
        if resolver is None:
            continue
        shim = ExtractedRelation(
            target_name=row["target_name"],
            kind=row["kind"],
            line=row["line"],
            column_start=row["column_start"],
            source_qname=row["source_qname"],
        )
        target_id = resolver(shim, row["language"], row["memory_id"], defs)
        if target_id is not None:
            conn.execute(
                "UPDATE code_relations SET target_symbol_id = ?, "
                "confidence = 'resolved' WHERE id = ?",
                (target_id, row["id"]),
            )
            result.relations_resolved += 1

    return result


# ───────────────────────────────────────────────────────────────────
# Synthetic <module> anchor
# ───────────────────────────────────────────────────────────────────


def _get_or_create_module_anchor(
    conn: sqlite3.Connection, file_memory_id: str, language: str,
) -> str:
    """Return the id of a ``code_symbols`` row representing the
    file-level scope. Created lazily on first reference per file.

    Kind is ``MODULE`` — outside the spec's documented enum
    (``FUNCTION | CLASS | METHOD | INTERFACE | STRUCT | ENUM | CONST``)
    but the column has no CHECK constraint, and tagging it distinctly
    lets agents filter it out of "real" symbol queries while still
    seeing it as the source of import relations.
    """
    row = conn.execute(
        "SELECT id FROM code_symbols WHERE file_memory_id = ? "
        "AND qualified_name = '<module>' AND kind = 'MODULE' LIMIT 1",
        (file_memory_id,),
    ).fetchone()
    if row is not None:
        return row["id"]

    new_id = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO code_symbols
           (id, file_memory_id, name, qualified_name, kind, language,
            signature, line_start, line_end, parent_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            new_id, file_memory_id, "<module>", "<module>", "MODULE",
            language, None, 1, 1, None, time.time(),
        ),
    )
    return new_id


# ───────────────────────────────────────────────────────────────────
# Pass 1 — build definitions table
# ───────────────────────────────────────────────────────────────────


def _build_definitions(
    conn: sqlite3.Connection, project_root: Path,
) -> DefinitionsTable:
    defs = DefinitionsTable()

    for row in conn.execute(
        "SELECT id, qualified_name, file_memory_id FROM code_symbols"
    ):
        memory_id = row["file_memory_id"]
        defs.per_file_symbols.setdefault(memory_id, {})
        defs.per_file_symbols[memory_id][row["qualified_name"]] = row["id"]

    for scan in conn.execute(
        "SELECT file_path, file_memory_id, language FROM codebase_scans"
    ):
        abs_path = Path(scan["file_path"])
        memory_id = scan["file_memory_id"]
        language = scan["language"]
        try:
            rel_path = str(abs_path.relative_to(project_root))
        except ValueError:
            continue
        defs.rel_path_by_memory[memory_id] = rel_path
        defs.memory_by_rel_path[rel_path] = memory_id
        defs.language_by_memory[memory_id] = language
        defs.dir_to_memory_ids.setdefault(
            str(Path(rel_path).parent), [],
        ).append(memory_id)

        # Python module path: drop .py and convert / to .; treat
        # __init__.py as the package itself (drop the trailing
        # `.__init__`).
        if language == "py":
            module_path = _python_module_path(rel_path)
            if module_path is not None:
                defs.file_by_module[(language, module_path)] = memory_id

    return defs


def _python_module_path(rel_path: str) -> str | None:
    """``foo/bar.py`` → ``foo.bar``;
    ``foo/bar/__init__.py`` → ``foo.bar``;
    Returns ``None`` for non-py paths.
    """
    p = Path(rel_path)
    if p.suffix != ".py":
        return None
    parts = list(p.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else None


# ───────────────────────────────────────────────────────────────────
# Grammar / query derivation (mirrors walker logic)
# ───────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────
# Python resolver
# ───────────────────────────────────────────────────────────────────
#
# Signature: ``(rel, language, memory_id, defs) -> target_symbol_id|None``.
# The resolver returns the target symbol's id if it can confidently
# map the relation's ``target_name``; otherwise None and the row is
# recorded as ``confidence='unresolved'``.


def _resolve_python(
    rel: ExtractedRelation,
    language: str,
    memory_id: str,
    defs: DefinitionsTable,
) -> str | None:
    target = rel.target_name

    # Relative imports (``from . import x`` → target=".x"; ``from
    # .a import b`` → target=".a.b"). Resolution would require
    # interpreting the source file's package — left for follow-on.
    if target.startswith("."):
        return None

    same_file_syms = defs.per_file_symbols.get(memory_id, {})

    if rel.kind == "imports":
        # ``from a.b import foo`` → target="a.b.foo". Split into
        # module path + last segment, look up the file, then the
        # symbol within it.
        if "." in target:
            module_path, _, leaf = target.rpartition(".")
            file_id = defs.file_by_module.get(("py", module_path))
            if file_id is not None:
                return defs.per_file_symbols.get(file_id, {}).get(leaf)
        # ``import a.b.c`` → target="a.b.c" (whole-module). No
        # specific symbol to link to; stays unresolved.
        return None

    if rel.kind == "calls":
        # Same-file bare call: target="foo" + symbol "foo" in source
        # file.
        if target in same_file_syms:
            return same_file_syms[target]

        # ``self.foo()`` inside a method → look up the method's
        # parent class and search for ``Class.foo`` in same file.
        if target.startswith("self."):
            leaf = target[len("self."):]
            class_qname = _enclosing_class_of_source(rel.source_qname)
            if class_qname is not None:
                candidate = f"{class_qname}.{leaf}"
                if candidate in same_file_syms:
                    return same_file_syms[candidate]
            return None

        # Aliased calls (``np.array``) and dotted calls without
        # alias tracking stay unresolved — alias-tracking would
        # require carrying the per-file import map into resolution,
        # which is a structurally larger change deferred to a
        # follow-on.
    return None


def _enclosing_class_of_source(source_qname: str) -> str | None:
    """Given a source_qname like ``Greeter.greet`` (METHOD inside
    class), return ``Greeter``. Returns None if the source has no
    class qualifier (top-level function, free method, etc.).
    """
    if "." not in source_qname:
        return None
    head, _, _ = source_qname.rpartition(".")
    return head or None


_Resolver = Callable[
    [ExtractedRelation, str, str, DefinitionsTable], "str | None"
]


# ───────────────────────────────────────────────────────────────────
# Shared helpers used by the 8 non-Python resolvers
# ───────────────────────────────────────────────────────────────────


def _same_file_lookup(
    rel: ExtractedRelation, memory_id: str, defs: DefinitionsTable,
) -> str | None:
    """Look up ``rel.target_name`` in the source file's symbols. The
    base resolution strategy for every language — Python builds on
    this, the rest of the languages add per-language cross-file logic
    on top.
    """
    same_file = defs.per_file_symbols.get(memory_id, {})
    return same_file.get(rel.target_name)


def _siblings_in_same_directory(
    memory_id: str, defs: DefinitionsTable,
) -> list[str]:
    """Return file_memory_ids of OTHER files in the same directory as
    ``memory_id``. Used by Go (same-package convention) and Java (same
    directory ≈ same package in single-rooted projects).

    P1-1: reads the precomputed ``dir_to_memory_ids`` map — the
    previous per-call scan of ``rel_path_by_memory`` was O(files) per
    relation, which matters now that re-resolution runs over every
    unresolved row from the DB.
    """
    source_rel = defs.rel_path_by_memory.get(memory_id)
    if source_rel is None:
        return []
    source_dir = str(Path(source_rel).parent)
    return [
        m for m in defs.dir_to_memory_ids.get(source_dir, [])
        if m != memory_id
    ]


# ───────────────────────────────────────────────────────────────────
# Languages that resolve same-file only
# ───────────────────────────────────────────────────────────────────


def _resolve_same_file_only(
    rel: ExtractedRelation, language: str, memory_id: str,
    defs: DefinitionsTable,
) -> str | None:
    """Used for Ruby / PHP / C / C++. Cross-file resolution for these
    languages would need either an autoload convention (PHP), the
    require search path (Ruby), or include-path tracking (C/C++) —
    deferred to follow-on per the plan's T9b scope.
    """
    return _same_file_lookup(rel, memory_id, defs)


# ───────────────────────────────────────────────────────────────────
# TypeScript / JavaScript resolver
# ───────────────────────────────────────────────────────────────────


_TS_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _resolve_ts_js(
    rel: ExtractedRelation, language: str, memory_id: str,
    defs: DefinitionsTable,
) -> str | None:
    same = _same_file_lookup(rel, memory_id, defs)
    if same is not None:
        return same
    if rel.kind == "imports":
        # Extractor target shape: ``./b.foo`` (relative module path +
        # imported name joined by ``.``). Split on last dot.
        if "." not in rel.target_name:
            return None
        module_specifier, _, leaf = rel.target_name.rpartition(".")
        if not (
            module_specifier.startswith("./")
            or module_specifier.startswith("../")
        ):
            # External package (``react``, ``@scope/pkg``, etc.). No
            # node_modules walking in v1.
            return None
        target_memory_id = _ts_resolve_relative_module(
            module_specifier, memory_id, defs,
        )
        if target_memory_id is None:
            return None
        return defs.per_file_symbols.get(target_memory_id, {}).get(leaf)
    return None


def _ts_resolve_relative_module(
    module_specifier: str, source_memory_id: str, defs: DefinitionsTable,
) -> str | None:
    """``./b`` from ``src/a.ts`` → look for ``src/b.ts`` /
    ``src/b.tsx`` / ``src/b/index.ts`` etc. Tries common TS/JS file
    extensions and ``index.ext`` fallbacks; returns the first match.
    """
    source_rel = defs.rel_path_by_memory.get(source_memory_id)
    if source_rel is None:
        return None
    source_dir = Path(source_rel).parent
    target_base = (source_dir / module_specifier).as_posix()

    # P1-1: precomputed rel_path → memory_id lookup (was rebuilt per
    # call — O(files) per relation).
    by_path = defs.memory_by_rel_path

    for ext in _TS_JS_EXTENSIONS:
        candidate = target_base + ext
        if candidate in by_path:
            return by_path[candidate]
        index_candidate = f"{target_base}/index{ext}"
        if index_candidate in by_path:
            return by_path[index_candidate]
    return None


# ───────────────────────────────────────────────────────────────────
# Go resolver (same-directory = same-package)
# ───────────────────────────────────────────────────────────────────


def _resolve_go(
    rel: ExtractedRelation, language: str, memory_id: str,
    defs: DefinitionsTable,
) -> str | None:
    same = _same_file_lookup(rel, memory_id, defs)
    if same is not None:
        return same
    if rel.kind == "calls":
        for sibling_id in _siblings_in_same_directory(memory_id, defs):
            sym = defs.per_file_symbols.get(sibling_id, {}).get(rel.target_name)
            if sym is not None:
                return sym
    return None


# ───────────────────────────────────────────────────────────────────
# Java resolver (same-directory = same-package by convention)
# ───────────────────────────────────────────────────────────────────


def _resolve_java(
    rel: ExtractedRelation, language: str, memory_id: str,
    defs: DefinitionsTable,
) -> str | None:
    same = _same_file_lookup(rel, memory_id, defs)
    if same is not None:
        return same
    if rel.kind == "calls":
        for sibling_id in _siblings_in_same_directory(memory_id, defs):
            sym = defs.per_file_symbols.get(sibling_id, {}).get(rel.target_name)
            if sym is not None:
                return sym
    return None


# ───────────────────────────────────────────────────────────────────
# Rust resolver (foo::bar → sibling file foo.rs)
# ───────────────────────────────────────────────────────────────────


def _resolve_rust(
    rel: ExtractedRelation, language: str, memory_id: str,
    defs: DefinitionsTable,
) -> str | None:
    same = _same_file_lookup(rel, memory_id, defs)
    if same is not None:
        return same
    # T11 Major #4 fix: Rust extractor emits ``Type::method`` for
    # scoped calls but symbols store ``Type.method`` qnames (uniform
    # cross-language convention). Try the `.`-normalized form against
    # the same-file index before falling through to cross-file mod
    # resolution — otherwise local `Point::new()` inside the file
    # that defines `Point` is always recorded as unresolved.
    if "::" in rel.target_name:
        normalized = rel.target_name.replace("::", ".")
        same_file_syms = defs.per_file_symbols.get(memory_id, {})
        hit = same_file_syms.get(normalized)
        if hit is not None:
            return hit
    if rel.kind == "calls" and "::" in rel.target_name:
        module_path, _, item = rel.target_name.rpartition("::")
        source_rel = defs.rel_path_by_memory.get(memory_id)
        if source_rel is None:
            return None
        source_dir = Path(source_rel).parent
        # Two convention-based candidates: ``module.rs`` (file-as-module)
        # or ``module/mod.rs`` (directory-as-module). For nested paths
        # like ``a::b::c`` we try the deepest candidate first.
        candidates = [
            (source_dir / f"{module_path.replace('::', '/')}.rs").as_posix(),
            (source_dir / module_path.replace("::", "/") / "mod.rs").as_posix(),
        ]
        by_path = defs.memory_by_rel_path
        for candidate in candidates:
            target_memory_id = by_path.get(candidate)
            if target_memory_id is not None:
                sym = defs.per_file_symbols.get(target_memory_id, {}).get(item)
                if sym is not None:
                    return sym
    return None


# ───────────────────────────────────────────────────────────────────
# Resolver registry
# ───────────────────────────────────────────────────────────────────


_RESOLVERS: dict[str, _Resolver] = {
    "py":   _resolve_python,
    "ts":   _resolve_ts_js,
    "js":   _resolve_ts_js,
    "go":   _resolve_go,
    "rs":   _resolve_rust,
    "java": _resolve_java,
    "rb":   _resolve_same_file_only,
    "php":  _resolve_same_file_only,
    "c":    _resolve_same_file_only,
    "cpp":  _resolve_same_file_only,
}
