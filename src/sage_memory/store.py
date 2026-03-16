"""Memory storage operations.

Store uses two-phase write:
  Phase 1: INSERT + FTS5 (synchronous, <1ms) → immediately keyword-searchable
  Phase 2: embed + vec INSERT (only if embedder quality ≥ threshold) → semantic search

Near-duplicate detection: when storing, if the content hash matches an existing
entry, returns the existing ID. The LLM can then decide to update instead.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

from .db import get_db
from .embedder import get_embedder, serialize_vec

_EMBED_QUALITY_THRESHOLD = 0.6


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Store
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def store(*, content: str, title: str | None = None,
          tags: list[str] | None = None, scope: str = "project") -> dict:
    """Store a memory. Returns {success, id, message}."""
    content = _normalize(content)
    if len(content) < 10:
        return {"success": False, "id": "", "message": "Content too short (min 10 chars)."}

    db = get_db(scope)
    now = time.time()
    memory_id = uuid.uuid4().hex
    title = (title or _auto_title(content)).strip()[:200]
    tags_json = json.dumps(sorted(set(t.lower().strip() for t in (tags or []) if t.strip())))
    content_hash = hashlib.sha256(content.encode()).hexdigest()

    # Dedup by content hash
    existing = db.execute(
        "SELECT id, title FROM memories WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    if existing:
        return {"success": False, "id": existing["id"],
                "message": f"Duplicate content exists: \"{existing['title']}\" (id={existing['id']})."}

    # Phase 1: write content + FTS5 (fast path)
    db.execute(
        """INSERT INTO memories
           (id, title, content, tags, content_hash, embedded,
            created_at, updated_at, accessed_at, access_count)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 0)""",
        (memory_id, title, content, tags_json, content_hash, now, now, now),
    )
    db.commit()

    # Phase 2: embed (only if worthwhile)
    _try_embed(db, memory_id, title, content)

    return {"success": True, "id": memory_id, "message": "Stored."}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Update
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def update(*, id: str, content: str | None = None, title: str | None = None,
           tags: list[str] | None = None, scope: str = "project") -> dict:
    """Partial update by ID. Content changes trigger re-embedding."""
    db = get_db(scope)
    row = db.execute("SELECT * FROM memories WHERE id = ?", (id,)).fetchone()
    if not row:
        return {"success": False, "message": f"Not found: {id}"}

    now = time.time()
    new_content = _normalize(content) if content else row["content"]
    new_title = title.strip()[:200] if title else row["title"]
    new_tags = json.dumps(sorted(set(
        t.lower().strip() for t in tags if t.strip()
    ))) if tags is not None else row["tags"]
    new_hash = hashlib.sha256(new_content.encode()).hexdigest()

    # Dedup (exclude self)
    dup = db.execute(
        "SELECT id FROM memories WHERE content_hash = ? AND id != ?",
        (new_hash, id),
    ).fetchone()
    if dup:
        return {"success": False, "message": f"Duplicate content (id={dup['id']})."}

    needs_reembed = content is not None or title is not None
    db.execute(
        """UPDATE memories SET title=?, content=?, tags=?,
           content_hash=?, embedded=?, updated_at=? WHERE id=?""",
        (new_title, new_content, new_tags, new_hash,
         0 if needs_reembed else row["embedded"], now, id),
    )

    if needs_reembed:
        db.execute("DELETE FROM memories_vec WHERE memory_id = ?", (id,))
        _try_embed(db, id, new_title, new_content)
    db.commit()

    return {"success": True, "id": id, "message": "Updated."}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Delete
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def delete(*, id: str, scope: str = "project") -> dict:
    """Delete a single memory by ID."""
    db = get_db(scope)
    row = db.execute("SELECT id FROM memories WHERE id = ?", (id,)).fetchone()
    if not row:
        return {"success": False, "deleted": 0, "message": "Not found."}

    db.execute("DELETE FROM memories_vec WHERE memory_id = ?", (id,))
    db.execute("DELETE FROM memories WHERE id = ?", (id,))
    db.commit()
    return {"success": True, "deleted": 1, "message": "Deleted."}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# List
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def list_memories(*, scope: str = "project", limit: int = 20, offset: int = 0) -> dict:
    """Browse stored memories with pagination."""
    db = get_db(scope)
    limit = max(1, min(limit, 100))

    total = db.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
    rows = db.execute(
        """SELECT id, title, tags, access_count, updated_at
           FROM memories ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
        (limit, max(0, offset)),
    ).fetchall()

    return {
        "items": [
            {"id": r["id"], "title": r["title"],
             "tags": json.loads(r["tags"]), "access_count": r["access_count"]}
            for r in rows
        ],
        "total": total,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Embed pending (called from search)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def embed_pending(db, batch_size: int = 50) -> int:
    """Embed memories missing vectors. Returns count processed."""
    embedder = get_embedder()
    if embedder.quality < _EMBED_QUALITY_THRESHOLD:
        return 0

    rows = db.execute(
        "SELECT id, title, content FROM memories WHERE embedded = 0 LIMIT ?",
        (batch_size,),
    ).fetchall()

    count = 0
    for r in rows:
        try:
            vec = embedder.embed(f"{r['title']}. {r['content']}")
            db.execute(
                "INSERT OR REPLACE INTO memories_vec (memory_id, embedding) VALUES (?, ?)",
                (r["id"], serialize_vec(vec)),
            )
            db.execute("UPDATE memories SET embedded = 1 WHERE id = ?", (r["id"],))
            count += 1
        except Exception:
            break
    if count:
        db.commit()
    return count


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _normalize(text: str) -> str:
    text = text.strip().replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", text)


def _auto_title(content: str, max_len: int = 80) -> str:
    for line in content.splitlines():
        line = line.strip().lstrip("#").strip()
        if len(line) >= 10:
            return line[:max_len]
    return content[:max_len].strip()


def _try_embed(db, memory_id: str, title: str, content: str) -> None:
    """Embed and store vector if embedder quality warrants it."""
    try:
        embedder = get_embedder()
        if embedder.quality >= _EMBED_QUALITY_THRESHOLD:
            vec = embedder.embed(f"{title}. {content}")
            db.execute(
                "INSERT INTO memories_vec (memory_id, embedding) VALUES (?, ?)",
                (memory_id, serialize_vec(vec)),
            )
            db.execute("UPDATE memories SET embedded = 1 WHERE id = ?", (memory_id,))
            db.commit()
    except Exception:
        pass
