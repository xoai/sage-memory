"""Graph operations: typed edges between memories and multi-hop traversal.

sage_memory_link  — create or delete edges
sage_memory_graph — cycle-safe traversal with depth limit

Edges are stored in the same database as the memories they connect.
CASCADE deletes clean up edges automatically when memories are removed.
"""

from __future__ import annotations

import json
import time
import uuid

from .db import get_db


def link(*, source_id: str, target_id: str, relation: str,
         properties: dict | None = None, delete: bool = False,
         scope: str = "project") -> dict:
    """Create or delete a typed edge between two memories."""
    db = get_db(scope)

    if delete:
        row = db.execute(
            "DELETE FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
            (source_id, target_id, relation),
        )
        db.commit()
        deleted = row.rowcount if hasattr(row, 'rowcount') else 0
        # Fallback: check if it existed
        if deleted == 0:
            existing = db.execute(
                "SELECT id FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
                (source_id, target_id, relation),
            ).fetchone()
            if existing:
                db.execute("DELETE FROM edges WHERE id = ?", (existing["id"],))
                db.commit()
                deleted = 1
        return {"success": deleted > 0, "deleted": deleted,
                "message": "Deleted." if deleted else "Edge not found."}

    # Validate both endpoints exist
    src = db.execute("SELECT id FROM memories WHERE id = ?", (source_id,)).fetchone()
    if not src:
        return {"success": False, "message": f"Source memory not found: {source_id}"}
    tgt = db.execute("SELECT id FROM memories WHERE id = ?", (target_id,)).fetchone()
    if not tgt:
        return {"success": False, "message": f"Target memory not found: {target_id}"}

    # Prevent self-loops
    if source_id == target_id:
        return {"success": False, "message": "Self-loops not allowed."}

    # Upsert: if edge exists, update properties
    existing = db.execute(
        "SELECT id FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
        (source_id, target_id, relation),
    ).fetchone()

    now = time.time()
    props_json = json.dumps(properties or {})

    if existing:
        db.execute(
            "UPDATE edges SET properties = ?, created_at = ? WHERE id = ?",
            (props_json, now, existing["id"]),
        )
        db.commit()
        return {"success": True, "id": existing["id"], "message": "Edge updated."}

    edge_id = uuid.uuid4().hex
    db.execute(
        """INSERT INTO edges (id, source_id, target_id, relation, properties, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (edge_id, source_id, target_id, relation, props_json, now),
    )
    db.commit()
    return {"success": True, "id": edge_id, "message": "Edge created."}


def graph(*, id: str, relation: str | None = None,
          direction: str = "outbound", depth: int = 1,
          scope: str = "project") -> dict:
    """Cycle-safe multi-hop traversal from a starting memory.

    direction: "outbound" (follow source→target), "inbound" (follow target→source),
               "both" (follow either direction)
    depth: max hops (1-5, default 1)
    relation: optional — filter to specific relation type

    Returns nodes (memories) and edges found during traversal, with paths.
    """
    db = get_db(scope)
    depth = max(1, min(depth, 5))

    # Verify starting node exists
    start = db.execute(
        "SELECT id, title, tags FROM memories WHERE id = ?", (id,)
    ).fetchone()
    if not start:
        return {"success": False, "message": f"Memory not found: {id}",
                "nodes": [], "edges": []}

    # Build traversal query based on direction
    if direction == "outbound":
        edge_sql = "SELECT id, source_id, target_id, relation, properties FROM edges WHERE source_id = ?"
        next_col = "target_id"
    elif direction == "inbound":
        edge_sql = "SELECT id, source_id, target_id, relation, properties FROM edges WHERE target_id = ?"
        next_col = "source_id"
    else:  # both
        edge_sql = ("SELECT id, source_id, target_id, relation, properties FROM edges "
                    "WHERE source_id = ? OR target_id = ?")
        next_col = None  # handled specially

    if relation:
        edge_sql += " AND relation = ?"

    # BFS traversal with cycle detection
    visited_nodes: set[str] = {id}
    visited_edges: set[str] = set()
    queue: list[tuple[str, int]] = [(id, 0)]  # (node_id, current_depth)
    result_nodes: list[dict] = []
    result_edges: list[dict] = []

    while queue:
        current_id, current_depth = queue.pop(0)

        if current_depth >= depth:
            continue

        # Fetch edges from current node
        if direction == "both":
            params = [current_id, current_id]
        else:
            params = [current_id]
        if relation:
            params.append(relation)

        edges = db.execute(edge_sql, params).fetchall()

        for edge in edges:
            edge_id = edge["id"]
            if edge_id in visited_edges:
                continue
            visited_edges.add(edge_id)

            # Determine the "other" node
            if direction == "outbound":
                next_id = edge["target_id"]
            elif direction == "inbound":
                next_id = edge["source_id"]
            else:  # both
                next_id = edge["target_id"] if edge["source_id"] == current_id else edge["source_id"]

            result_edges.append({
                "id": edge_id,
                "source_id": edge["source_id"],
                "target_id": edge["target_id"],
                "relation": edge["relation"],
                "properties": json.loads(edge["properties"]),
            })

            if next_id not in visited_nodes:
                visited_nodes.add(next_id)
                queue.append((next_id, current_depth + 1))

    # Fetch full memory data for all discovered nodes (excluding start)
    discovered_ids = list(visited_nodes - {id})
    if discovered_ids:
        ph = ",".join("?" for _ in discovered_ids)
        rows = db.execute(
            f"SELECT id, title, content, tags FROM memories WHERE id IN ({ph})",
            discovered_ids,
        ).fetchall()
        for r in rows:
            result_nodes.append({
                "id": r["id"],
                "title": r["title"],
                "content": r["content"],
                "tags": json.loads(r["tags"]) if isinstance(r["tags"], str) else r["tags"],
            })

        # Bump access tracking for all traversed nodes
        now = time.time()
        try:
            db.execute(
                f"""UPDATE memories SET accessed_at = ?, access_count = access_count + 1
                    WHERE id IN ({ph})""",
                [now] + discovered_ids,
            )
            db.commit()
        except Exception:
            pass

    return {
        "success": True,
        "start": {"id": start["id"], "title": start["title"]},
        "nodes": result_nodes,
        "edges": result_edges,
        "depth": depth,
        "direction": direction,
    }
