"""sage-memory MCP server.

8 tools, namespaced to avoid collision with client built-in memory:
  sage_memory_set_project — set the active project for this session
  sage_memory_store       — persist understanding, decisions, patterns
  sage_memory_search      — find relevant knowledge across project + global
  sage_memory_update      — refine existing knowledge
  sage_memory_delete      — remove outdated knowledge
  sage_memory_list        — browse what's stored
  sage_memory_link        — create typed edges between memories
  sage_memory_graph       — traverse relationships across memories

Tool descriptions guide the LLM to produce high-quality, retrievable content.
"""

from __future__ import annotations

import json
import logging

from mcp.server import Server
import mcp.types as types

from .store import store, update, delete, list_memories
from .search import search
from .graph import link, graph
from .db import get_project_name, set_project, get_db
from .embedder import get_embedder
from . import llm

logger = logging.getLogger("sage-memory")

TOOLS = [
    types.Tool(
        name="sage_memory_set_project",
        description=(
            "Set the active project for this session. Call this FIRST before "
            "any other sage_memory tools, passing the current project's root "
            "directory path. This ensures all stores and searches use the "
            "correct project database. Without this call, sage-memory falls "
            "back to detecting the project from the server's working directory, "
            "which may be stale if the server was started from a different project."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute path to the project root directory. "
                        "This is typically the directory containing .git, "
                        "pyproject.toml, package.json, or similar markers."
                    ),
                },
            },
            "required": ["path"],
        },
    ),
    types.Tool(
        name="sage_memory_store",
        description=(
            "Store knowledge for future retrieval. Use this to persist: "
            "code understanding (architecture, patterns, data flows), "
            "decisions and their rationale, "
            "debugging insights and solutions, "
            "project conventions and rules. "
            "Write a clear, descriptive title (what is this about?) and "
            "detailed content explaining the 'what' and 'why'. "
            "Good content is specific, uses domain vocabulary, and would "
            "help someone (or you, later) understand the topic without "
            "reading the source code. "
            "0.9+: pass optional `entities` and `relations` to populate "
            "the knowledge graph inline (no LLM API key needed for "
            "sage-memory). Response includes `suggested_links` listing "
            "existing memories whose content overlaps. "
            "0.12.0+: `suggested_links` entries may carry "
            "`confidence: \"near_duplicate\"` and `similarity` when the "
            "new memory is semantically near a stored one (cosine ≥ 0.95). "
            "When this signals fires, consider linking via "
            "`relation: \"supersedes\"` to mark the older memory as "
            "superseded by the newer paraphrase. "
            "Best fit: durable context — architecture decisions, user "
            "preferences, debugging insights, conventions, prevention "
            "rules. Anything an agent benefits from recalling next session."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "Detailed explanation of the knowledge. Include: "
                        "what it does, why it matters, key patterns or gotchas. "
                        "Use the project's actual terminology — class names, "
                        "function names, domain concepts. Markdown supported."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Short descriptive title (5-15 words). Be specific: "
                        "'Payment saga orchestration in billing service' not "
                        "'How payments work'."
                    ),
                },
                "tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Domain tags for filtering: technology, area, concept.",
                },
                "scope": {
                    "type": "string", "enum": ["project", "global"],
                    "description": (
                        "'project' (default) for this codebase's knowledge, "
                        "'global' for cross-project patterns and preferences."
                    ),
                },
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "type": {
                                "type": "string",
                                "enum": ["PERSON", "CONCEPT", "TECHNOLOGY",
                                         "PROJECT", "EVENT", "OTHER"],
                            },
                            "surface_form": {"type": "string"},
                        },
                        "required": ["name", "type"],
                    },
                    "description": (
                        "Entities mentioned in the content (0.9+). Populates "
                        "the knowledge graph without sage-memory needing its "
                        "own LLM key. Max 50 entries per call."
                    ),
                },
                "relations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "from": {"type": "string"},
                            "to": {"type": "string"},
                            "rel": {
                                "type": "string",
                                "enum": ["mentions", "relates_to", "contains",
                                         "depends_on", "contradicts",
                                         "derived_from", "implements",
                                         "references", "supersedes",
                                         "alternative_to"],
                            },
                        },
                        "required": ["from", "to", "rel"],
                    },
                    "description": (
                        "Relations between entities (0.9+). `from`/`to` are "
                        "entity names matched against entities passed in this "
                        "call AND existing entities in the DB. Unresolvable "
                        "endpoints are silently dropped. Max 100 entries."
                    ),
                },
            },
            "required": ["content"],
        },
    ),
    types.Tool(
        name="sage_memory_search",
        description=(
            "Search stored knowledge using natural language. "
            "Searches this project's memory and global memory, "
            "with project results ranked higher. "
            "Use before starting work to recall relevant context, "
            "architecture decisions, or past solutions. "
            "After running scan-codebase, combine "
            "filter_tags: ['codebase'] with code-specific queries to "
            "discover files by topic. "
            "0.12.0+: result entries may carry `superseded_by: <id>` "
            "when a newer memory has been linked via "
            "`relation: \"supersedes\"`. Results are NOT filtered or "
            "down-ranked — the agent decides whether to prefer the "
            "newer memory."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What you're looking for — describe the topic or question naturally.",
                },
                "tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Soft boost — results with these tags rank higher, but non-matching results are still included.",
                },
                "filter_tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": (
                        "Hard filter (AND logic) — ONLY return memories matching ALL these tags. "
                        "Use for namespace isolation, e.g. filter_tags: [\"self-learning\"] "
                        "to search only within learnings, or filter_tags: [\"codebase\"] "
                        "to surface only source-file memories after sage_memory_scan_codebase."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (1-50, default 5).",
                },
                "scope": {
                    "type": "string", "enum": ["project", "global"],
                    "description": "'project' (default) searches project + global. 'global' searches global only.",
                },
                "strategy": {
                    "type": "string",
                    "enum": ["hybrid", "semantic", "keyword"],
                    "description": (
                        "Retrieval strategy: 'hybrid' (default) "
                        "combines FTS5 + vector; 'semantic' biases "
                        "toward vector; 'keyword' uses FTS5 only."
                    ),
                },
                "channels": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["bm25", "vector", "graph"],
                    },
                    "description": (
                        "Subset of channels to consult. Default = all "
                        "available (FTS5 + vector + graph proximity, "
                        "gracefully degrading when graph is empty). "
                        "Empty list returns no results."
                    ),
                },
                "expand": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Enable LLM query expansion. Default: false "
                        "(matches published 0.8.0 bench config). Pass "
                        "true to enable; requires LLM API key."
                    ),
                },
                "rerank": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Enable LLM rerank of top-K results. Default: "
                        "false (matches published 0.8.0 bench config). "
                        "Pass true to enable; requires LLM API key."
                    ),
                },
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="sage_memory_update",
        description=(
            "Update existing knowledge by ID. Use when understanding deepens, "
            "code changes, or stored information becomes outdated. "
            "Only provide fields you want to change. "
            "0.9+: passing `entities` (including `[]`) REPLACEs all mentions "
            "and source-relations for this memory; omit to leave the graph "
            "rows untouched. Response includes `suggested_links`."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory ID to update."},
                "content": {"type": "string", "description": "New content."},
                "title": {"type": "string", "description": "New title."},
                "tags": {"type": "array", "items": {"type": "string"}},
                "status": {
                    "type": "string", "enum": ["active", "invalidated", "archived"],
                    "description": (
                        "Lifecycle status. Set to 'invalidated' when a learning is "
                        "proven wrong — it will be excluded from all future searches."
                    ),
                },
                "scope": {
                    "type": "string", "enum": ["project", "global"],
                    "description": "Which database contains this memory.",
                },
                "entities": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Agent-provided entities for this memory (0.9+). When "
                        "passed (including `[]`), REPLACEs prior mentions and "
                        "source-relations regardless of origin. Same shape as "
                        "sage_memory_store."
                    ),
                },
                "relations": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Agent-provided relations (0.9+). Same shape as "
                        "sage_memory_store."
                    ),
                },
            },
            "required": ["id"],
        },
    ),
    types.Tool(
        name="sage_memory_delete",
        description="Delete a memory by ID. Use when knowledge is no longer relevant.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory ID to delete."},
                "scope": {
                    "type": "string", "enum": ["project", "global"],
                    "description": "Which database contains this memory.",
                },
            },
            "required": ["id"],
        },
    ),
    types.Tool(
        name="sage_memory_list",
        description=(
            "Browse stored memories with optional tag filtering. "
            "Shows active knowledge by default, sorted by most recently updated. "
            "Tags use AND logic: all specified tags must match. "
            "Set include_archived to see invalidated/archived memories too."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string", "enum": ["project", "global"],
                    "description": "Which database to browse (default: project).",
                },
                "tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Filter by tags (AND logic — all must match).",
                },
                "limit": {"type": "integer", "description": "Page size (default 20)."},
                "offset": {"type": "integer", "description": "Pagination offset."},
                "include_archived": {
                    "type": "boolean",
                    "description": "If true, also show invalidated and archived memories. Default false.",
                },
            },
        },
    ),
    types.Tool(
        name="sage_memory_link",
        description=(
            "Create or delete a typed relationship (edge) between two memories. "
            "Use this to express: dependencies (A depends_on B), containment "
            "(project has_task task), ownership (task assigned_to person), "
            "blocking (task blocks task), or any directed relationship. "
            "Edges are automatically cleaned up when either memory is deleted. "
            "Best fit: dependency graphs (A depends on B), supersession "
            "chains (new memory replaces an outdated one), task→entity "
            "associations, learning→affected-object pointers."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "source_id": {"type": "string", "description": "ID of the source memory (edge starts here)."},
                "target_id": {"type": "string", "description": "ID of the target memory (edge points here)."},
                "relation": {
                    "type": "string",
                    "description": (
                        "Relationship type. Common relations: depends_on, "
                        "has_task, assigned_to, blocks, part_of, contains, "
                        "relates_to, supersedes (newer memory replaces an "
                        "older paraphrase — 0.12.0+, see sage_memory_store's "
                        "`confidence: \"near_duplicate\"` signal), or any "
                        "custom string."
                    ),
                },
                "properties": {
                    "type": "object",
                    "description": "Optional JSON properties on the edge (confidence, notes, etc.).",
                },
                "delete": {
                    "type": "boolean",
                    "description": "If true, delete the edge instead of creating it.",
                },
                "scope": {
                    "type": "string", "enum": ["project", "global"],
                    "description": "Which database (default: project).",
                },
            },
            "required": ["source_id", "target_id", "relation"],
        },
    ),
    types.Tool(
        name="sage_memory_graph",
        description=(
            "Traverse relationships from a starting memory. Returns connected "
            "memories and edges within the specified depth. Use to explore: "
            "dependency chains, project task trees, blocking relationships, "
            "or any graph structure built with sage_memory_link. "
            "Best fit: cross-entity impact analysis ('what depends on X?'), "
            "tracing supersession chains, surfacing all learnings touching "
            "a given module/topic."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Starting memory ID."},
                "relation": {
                    "type": "string",
                    "description": "Optional — only follow edges of this relation type.",
                },
                "direction": {
                    "type": "string", "enum": ["outbound", "inbound", "both"],
                    "description": "outbound (source→target), inbound (target→source), or both. Default: outbound.",
                },
                "depth": {
                    "type": "integer",
                    "description": "Max traversal hops (1-5, default 1).",
                },
                "scope": {
                    "type": "string", "enum": ["project", "global"],
                    "description": "Which database (default: project).",
                },
            },
            "required": ["id"],
        },
    ),
    types.Tool(
        name="sage_memory_scan_codebase",
        description=(
            "Scan local source code with tree-sitter; populates the "
            "project's code-symbol index. Requires the [codebase] pip "
            "extra (install with: pip install 'sage-memory[codebase]'). "
            "Listed unconditionally so agents know to check install "
            "state via the call's success/false envelope rather than "
            "tool-list absence. "
            "Best fit: code intelligence without sending source to a "
            "SaaS — keeps proprietary code local."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Directory to scan (defaults to project root)."
                    ),
                },
                "languages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Subset of {py,ts,js,go,rs,java,rb,php,c,cpp}. "
                        "Default: all detected by file extension."
                    ),
                },
                "include_ignored": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 5000},
                "force": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": False},
            },
        },
    ),
    # P2-1 (SM-CAP-01): structural code-graph queries. ADDITIVE —
    # the existing 10 tools are untouched (invariant 4). All three
    # require the [codebase] extra and a prior scan-codebase run.
    types.Tool(
        name="sage_memory_code_path",
        description=(
            "Shortest call/import path between two code symbols over "
            "the RESOLVED edges of the scanned code graph (name-matched "
            "unresolved edges are never used as hops — a path through "
            "a guess would be false provenance). Ambiguous names return "
            "candidates instead of guessing. Requires a prior "
            "sage_memory_scan_codebase run."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "a": {
                    "type": "string",
                    "description": "Source symbol (name or qualified name).",
                },
                "b": {
                    "type": "string",
                    "description": "Target symbol (name or qualified name).",
                },
                "max_depth": {
                    "type": "integer", "default": 16,
                    "description": "BFS depth cap (max 16).",
                },
            },
            "required": ["a", "b"],
        },
    ),
    types.Tool(
        name="sage_memory_code_affected",
        description=(
            "What depends on a code symbol (reverse traversal of the "
            "scanned code graph) — the 'what breaks if I change this' "
            "query. Grouped by relation kind with file:line; resolved "
            "edges are facts, unresolved edges are labelled name-match. "
            "Requires a prior sage_memory_scan_codebase run."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Symbol to analyze (name or qualified name).",
                },
                "depth": {
                    "type": "integer", "default": 2,
                    "description": "Inbound traversal depth (max 8).",
                },
                "resolved_only": {
                    "type": "boolean", "default": False,
                    "description": (
                        "Exclude unresolved name-match edges."
                    ),
                },
            },
            "required": ["symbol"],
        },
    ),
    types.Tool(
        name="sage_memory_code_hubs",
        description=(
            "Most-connected code symbols (architectural hot spots) "
            "with in/out degree split. Requires a prior "
            "sage_memory_scan_codebase run."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer", "default": 20,
                    "description": "Max hubs returned (1-200).",
                },
                "resolved_only": {
                    "type": "boolean", "default": False,
                    "description": (
                        "Count only resolved edges in the degree."
                    ),
                },
            },
        },
    ),
]


def scan_codebase(
    path: str | None = None,
    languages: list[str] | None = None,
    include_ignored: bool = False,
    limit: int = 5000,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """MCP handler for ``sage_memory_scan_codebase``.

    Envelope shape matches the spec rev 2 convention:
      success → ``{success: true, files: {...}, symbols: {...},
                    relations: {...}, elapsed_ms: int}``
      failure → ``{success: false, message: str}`` (rev 2 align)
    """
    try:
        from .codebase import scan
    except ImportError:
        return _scan_failure_envelope(
            "scan-codebase requires the [codebase] extra. "
            "Install with: pip install 'sage-memory[codebase]' "
            "(or with uvx, set args to ['sage-memory[codebase]'])."
        )
    # Translate scan() exceptions into the rev 2 failure envelope so
    # agents see a consistent `{success: false, message: str}` shape
    # regardless of which safety guard tripped.
    from .codebase import (
        ScanLimitExceeded, ScanLockHeld, ScanRefused,
    )

    try:
        result = scan(
            root=path,
            languages=languages,
            include_ignored=include_ignored,
            limit=limit,
            force=force,
            dry_run=dry_run,
        )
    except (ScanLimitExceeded, ScanLockHeld, ScanRefused) as exc:
        return _scan_failure_envelope(str(exc))
    except RuntimeError as exc:
        return _scan_failure_envelope(str(exc))

    return {
        "success": True,
        "project_root": result.project_root,
        "languages_detected": list(result.languages_detected),
        "files": {
            "scanned":         result.files_scanned,
            "changed":         result.files_changed,
            "unchanged":       result.files_unchanged,
            "with_error_nodes": result.files_with_error_nodes,
        },
        "symbols": dict(result.symbols_by_kind),
        "relations": {
            "imports":          result.relations_imports,
            "calls_resolved":   result.relations_calls_resolved,
            "calls_unresolved": result.relations_calls_unresolved,
        },
        "parse_errors": result.parse_errors,
        "elapsed_ms":   result.elapsed_ms,
        "dry_run":      result.dry_run,
    }


def _scan_failure_envelope(message: str) -> dict:
    return {"success": False, "message": message}


# Dict-based dispatch
def _code_graph_failure(message: str) -> dict:
    return {"success": False, "message": message}


def _code_graph_conn():
    """Shared gate for the P2-1 code-graph tools: [codebase] extra +
    active project DB, mirroring scan_codebase's failure-envelope
    convention."""
    from .db import get_project_db
    conn = get_project_db()
    return conn


def code_path(a: str, b: str, max_depth: int = 16) -> dict:
    """MCP handler for ``sage_memory_code_path`` (P2-1)."""
    try:
        from .codebase.queries import find_path
    except ImportError:
        return _code_graph_failure(
            "code_path requires the [codebase] extra. "
            "Install with: pip install 'sage-memory[codebase]'."
        )
    conn = _code_graph_conn()
    if conn is None:
        return _code_graph_failure(
            "no active project — call sage_memory_set_project first"
        )
    return find_path(conn, a, b, max_depth=max_depth)


def code_affected(
    symbol: str, depth: int = 2, resolved_only: bool = False,
) -> dict:
    """MCP handler for ``sage_memory_code_affected`` (P2-1)."""
    try:
        from .codebase.queries import affected
    except ImportError:
        return _code_graph_failure(
            "code_affected requires the [codebase] extra. "
            "Install with: pip install 'sage-memory[codebase]'."
        )
    conn = _code_graph_conn()
    if conn is None:
        return _code_graph_failure(
            "no active project — call sage_memory_set_project first"
        )
    return affected(conn, symbol, depth=depth, resolved_only=resolved_only)


def code_hubs(limit: int = 20, resolved_only: bool = False) -> dict:
    """MCP handler for ``sage_memory_code_hubs`` (P2-1)."""
    try:
        from .codebase.queries import hubs
    except ImportError:
        return _code_graph_failure(
            "code_hubs requires the [codebase] extra. "
            "Install with: pip install 'sage-memory[codebase]'."
        )
    conn = _code_graph_conn()
    if conn is None:
        return _code_graph_failure(
            "no active project — call sage_memory_set_project first"
        )
    return hubs(conn, limit=limit, resolved_only=resolved_only)


HANDLERS = {
    "sage_memory_set_project": set_project,
    "sage_memory_store": store,
    "sage_memory_search": search,
    "sage_memory_update": update,
    "sage_memory_delete": delete,
    "sage_memory_list": list_memories,
    "sage_memory_link": link,
    "sage_memory_graph": graph,
    "sage_memory_scan_codebase": scan_codebase,
    "sage_memory_code_path": code_path,
    "sage_memory_code_affected": code_affected,
    "sage_memory_code_hubs": code_hubs,
}


def create_server() -> Server:
    server = Server("sage-memory")

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return TOOLS

    @server.call_tool()
    async def _call(name: str, arguments: dict) -> list[types.TextContent]:
        handler = HANDLERS.get(name)
        if not handler:
            return [types.TextContent(type="text",
                    text=json.dumps({"error": f"Unknown tool: {name}"}))]
        try:
            result = handler(**(arguments or {}))

            # Enrich response with project context
            if name in ("sage_memory_store", "sage_memory_search",
                       "sage_memory_list", "sage_memory_set_project"):
                project = get_project_name()
                if project:
                    result["_project"] = project

            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
        except Exception as e:
            logger.exception("Tool error: %s", name)
            return [types.TextContent(type="text",
                    text=json.dumps({"error": str(e)}))]

    return server


def _needs_worker(db) -> bool:
    """Determine whether the background worker should start.

    Worker starts if ANY of:
      (1) LLM key configured AND corpus_dim matches active embedder
          (extraction.enabled resolves to True)
      (2) Any memories row has embedded=1 but missing/mismatched
          memory_embedding_meta (stale memory-level embedding)
      (3) Any chunks row has missing/mismatched chunk_embedding_meta
          (stale chunk-level embedding)
      (4) dedup.enabled (always False in M3a — M5)
    """
    embedder = get_embedder()

    # (1) Extraction-enabled path
    if llm.is_configured():
        corpus_row = db.execute(
            "SELECT value FROM corpus_meta WHERE key = 'vec_dim'"
        ).fetchone()
        if corpus_row and int(corpus_row[0]) == embedder.dim:
            return True

    params = {
        "name": embedder.name,
        "version": embedder.version,
        "dim": embedder.dim,
    }

    # (2) Stale memory-level embedding
    stale_mem = db.execute(
        """
        SELECT 1 FROM memories m
          LEFT JOIN memory_embedding_meta em ON em.memory_id = m.id
         WHERE m.embedded = 1
           AND ((em.memory_id IS NULL)
                OR (em.dim != :dim)
                OR (em.model_name != :name)
                OR (em.model_version != :version))
         LIMIT 1
        """,
        params,
    ).fetchone()
    if stale_mem is not None:
        return True

    # (3) Stale chunk-level embedding
    stale_chunk = db.execute(
        """
        SELECT 1 FROM chunks c
          LEFT JOIN chunk_embedding_meta cm ON cm.chunk_id = c.id
         WHERE (cm.chunk_id IS NULL)
            OR (cm.dim != :dim)
            OR (cm.model_name != :name)
            OR (cm.model_version != :version)
         LIMIT 1
        """,
        params,
    ).fetchone()
    if stale_chunk is not None:
        return True

    # (4) dedup.enabled — M5
    return False


def _resolve_db_path() -> str | None:
    """Best-effort current project DB path for the worker.

    Returns None if no project is active (in which case the worker
    isn't started — the MCP server still runs in stdio-only mode).
    """
    try:
        conn = get_db()
    except Exception:
        return None
    # sqlite3.Connection doesn't expose the file path directly; pull
    # from PRAGMA database_list. The first row is always `main`
    # (seq=0) for SQLite, so fetchone() is the right path. Row shape
    # is (seq, name, file); row[2] is the file path. An in-memory DB
    # returns empty string — coalesce to None.
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None:
        return None
    return row[2] or None


async def run() -> None:
    """Thin wrapper around the FastMCP factory's stdio entry.

    M1.1b extracted the embedder bootstrap + worker probe + cleanup
    into ``server_fastmcp.server_lifespan`` (wired by ``build_mcp_app``),
    so this function is now a one-liner over FastMCP's stdio transport.
    The arg-less ``sage-memory`` entry point still routes here for
    backwards compatibility with existing MCP client configurations.
    """
    from .server_fastmcp import build_mcp_app
    mcp = build_mcp_app()
    await mcp.run_stdio_async()
