"""Cross-tool code-graph import — P2-4 (SM-CAP-01 adjacent).

Imports an external tool's deterministic code graph (a ``graph.json``
artifact of nodes/edges) into ``code_symbols`` / ``code_relations``.
Design: ``.sage/docs/design/graph-import.md`` (internal).

Load-bearing rules:
  - **File artifact only.** The two ecosystems pin incompatible
    tree-sitter versions and cannot share one Python environment —
    this module is a pure data interchange with NO dependency on any
    external package.
  - **No silent confidence upgrades.** Only explicit fact labels
    (``resolved``/``exact``/``verified``/``true``) become
    ``resolved``; everything else lands in ``unresolved``.
  - **Provenance.** Imported rows carry ``source = 'import:<tool>'``
    (migration 013); native rows keep the ``'native'`` DEFAULT.
  - **Idempotent per-source replace.** Re-importing from the same
    tool deletes that source's rows first — native and other tools'
    rows are never touched.
  - **Resilient.** Malformed entries are counted and skipped, never
    fatal (same contract as the scan loop).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


# Explicit fact labels — the ONLY external confidence values that may
# become sage-memory 'resolved' (design brief §Confidence mapping).
_FACT_LABELS = frozenset({"resolved", "exact", "verified", "true"})

_REQUIRED_NODE_KEYS = ("id", "name", "file")
_REQUIRED_EDGE_KEYS = ("source", "target", "kind")


def _map_confidence(external: Any) -> str:
    if isinstance(external, str) and external.lower() in _FACT_LABELS:
        return "resolved"
    return "unresolved"


def import_graph(
    conn: sqlite3.Connection,
    artifact_path: str | Path,
    *,
    tool: str | None = None,
) -> dict[str, Any]:
    """Import a ``graph.json`` artifact. See module docstring."""
    artifact_path = Path(artifact_path)
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))

    tool_name = tool or payload.get("tool") or "unknown"
    source = f"import:{tool_name}"
    nodes = payload.get("nodes") or []
    edges = payload.get("edges") or []

    result: dict[str, Any] = {
        "success": True,
        "tool": tool_name,
        "imported": 0,
        "skipped_malformed": 0,
        "edges_resolved": 0,
        "edges_unresolved": 0,
    }

    now = time.time()

    # Idempotent per-source replace: this source's previous rows go;
    # native and other tools' rows are never touched (design §Idempotency).
    conn.execute("DELETE FROM code_relations WHERE source = ?", (source,))

    # ── Nodes → code_symbols (+ file memory linkage) ──────────────
    ext_to_symbol: dict[str, str] = {}
    file_memories: dict[str, str] = {}  # artifact file → memory_id

    for node in nodes:
        if not all(k in node for k in _REQUIRED_NODE_KEYS):
            result["skipped_malformed"] += 1
            continue
        ext_id = node["id"]
        name = node["name"]
        qualified = node.get("qualified_name") or name
        file_rel = node["file"]

        memory_id = file_memories.get(file_rel)
        if memory_id is None:
            memory_id = _upsert_import_file_memory(
                conn, file_rel=file_rel, tool=tool_name, now=now,
            )
            file_memories[file_rel] = memory_id

        symbol_id = uuid.uuid4().hex
        # Re-import must not duplicate symbols for the same file+qname:
        # the tool's file memories are per-tool (sentinel hash), so
        # every symbol under them belongs to this import source.
        # Relations were already deleted above (per-source replace),
        # so the source-side FK cascade is a no-op here.
        conn.execute(
            """DELETE FROM code_symbols
               WHERE file_memory_id = ? AND qualified_name = ?""",
            (memory_id, qualified),
        )
        conn.execute(
            """INSERT INTO code_symbols
               (id, file_memory_id, name, qualified_name, kind, language,
                signature, line_start, line_end, parent_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                symbol_id, memory_id, name, qualified,
                node.get("kind", "FUNCTION"),
                node.get("language", "unknown"),
                node.get("signature"),
                int(node.get("line_start", 1)),
                int(node.get("line_end", node.get("line_start", 1))),
                None, now,
            ),
        )
        ext_to_symbol[ext_id] = symbol_id
        result["imported"] += 1

    # ── Edges → code_relations (with provenance + confidence map) ─
    for edge in edges:
        if not all(k in edge for k in _REQUIRED_EDGE_KEYS):
            result["skipped_malformed"] += 1
            continue
        source_id = ext_to_symbol.get(edge["source"])
        if source_id is None:
            result["skipped_malformed"] += 1
            continue
        # Endpoint that can't map stays visible with NULL target —
        # never dropped silently (same philosophy as native resolve).
        target_id = ext_to_symbol.get(edge["target"])
        target_name = (
            edge.get("target_name")
            or _node_name(nodes, edge["target"])
            or str(edge["target"])
        )
        confidence = _map_confidence(edge.get("confidence"))
        # A 'resolved' confidence REQUIRES a mapped target; an
        # un-mapped endpoint can never be a resolved fact.
        if target_id is None:
            confidence = "unresolved"

        conn.execute(
            """INSERT OR IGNORE INTO code_relations
               (id, source_symbol_id, target_symbol_id, target_name,
                kind, confidence, line, column_start, created_at, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uuid.uuid4().hex, source_id, target_id, target_name,
                edge["kind"], confidence,
                int(edge.get("line", 1)), 0, now, source,
            ),
        )
        if confidence == "resolved":
            result["edges_resolved"] += 1
        else:
            result["edges_unresolved"] += 1

    conn.commit()
    return result


def _node_name(nodes: list[dict], ext_id: Any) -> str | None:
    for n in nodes:
        if n.get("id") == ext_id:
            return n.get("name")
    return None


def _upsert_import_file_memory(
    conn: sqlite3.Connection, *, file_rel: str, tool: str, now: float,
) -> str:
    """Upsert the file memory + codebase_scans row for an imported
    file (design §File memory linkage). The content_hash sentinel
    (``import:<tool>:<file>``) satisfies NOT NULL, marks provenance,
    and can never collide with a real content hash.

    Does NOT read the file from disk — imported artifacts commonly
    reference files that don't exist locally (different machine,
    different language). The memory row is a linkage stub, not
    content.
    """
    sentinel = f"import:{tool}:{file_rel}"
    existing = conn.execute(
        "SELECT id FROM memories WHERE content_hash = ?", (sentinel,),
    ).fetchone()
    if existing is not None:
        memory_id = existing[0]
    else:
        import json as _json
        memory_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO memories
               (id, title, content, tags, content_hash, embedded,
                created_at, updated_at, accessed_at, access_count)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 0)""",
            (
                memory_id,
                f"[file:import] {file_rel}",
                f"Imported code-graph node file (tool: {tool})",
                _json.dumps(["codebase", "file", "import"]),
                sentinel, now, now, now,
            ),
        )
    conn.execute(
        """INSERT OR REPLACE INTO codebase_scans
           (file_path, content_hash, file_memory_id, language,
            last_scanned)
           VALUES (?, ?, ?, ?, ?)""",
        (file_rel, sentinel, memory_id, "import", now),
    )
    return memory_id
