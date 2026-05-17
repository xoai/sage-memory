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

from .chunker import (
    split as chunker_split,
    CHUNK_THRESHOLD,
    HYSTERESIS_LOW,
    MAX_CHUNKS_PER_MEMORY,
)
from .db import get_db
from .embedder import get_embedder, serialize_vec
from . import llm as _llm

_EMBED_QUALITY_THRESHOLD = 0.6


def _embedder_meets_threshold(embedder) -> bool:
    """Single source of truth for the embed-quality gate.

    Used by all four embed-touching call sites in this module plus
    M3a's enqueue gate in `store.store()` / `store.update()`.
    """
    return embedder.quality >= _EMBED_QUALITY_THRESHOLD


def _enqueue_extract(db, memory_id: str, content: str, now: float) -> None:
    """M3a (T5): enqueue an `extract` task for the worker if an LLM
    key is configured AND content length exceeds the per-spec floor
    (50 chars). Free-path floor: no LLM key → no enqueue, behavior
    matches M2.
    """
    if not _llm.is_configured() or len(content) <= 50:
        return
    db.execute(
        "INSERT INTO extraction_queue (id, memory_id, task_type, "
        "status, attempts, created_at) "
        "VALUES (?, ?, 'extract', 'pending', 0, ?)",
        (uuid.uuid4().hex, memory_id, now),
    )


def _enqueue_reembed(db, memory_id: str, now: float) -> None:
    """M3a (T5): enqueue a per-memory `reembed` task. Worker drains
    via T6a's memory_id-filtered embed_pending* (per rev 2 pin #1).
    Gated on the same quality threshold the embed functions use, so
    we don't enqueue work that the worker would no-op on."""
    if not _embedder_meets_threshold(get_embedder()):
        return
    db.execute(
        "INSERT INTO extraction_queue (id, memory_id, task_type, "
        "status, attempts, created_at) "
        "VALUES (?, ?, 'reembed', 'pending', 0, ?)",
        (uuid.uuid4().hex, memory_id, now),
    )


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

    # Phase 2: embed memory-level vec (synchronous, M1 path).
    # Chunk-level embedding moves async to the worker (M3a / T5).
    _try_embed(db, memory_id, title, content)

    # Phase 3: chunk if long (M2). Chunker is a no-op for short content.
    # Returns chunk count; the inline _try_embed_chunk loop was removed
    # in T5 — chunks_vec writes happen via the worker's reembed task.
    n_chunks = _chunk_and_embed(db, memory_id, content, now)

    # Phase 4: enqueue worker tasks (M3a / T5).
    _enqueue_extract(db, memory_id, content, now)
    if n_chunks > 0:
        _enqueue_reembed(db, memory_id, now)
    db.commit()

    return {"success": True, "id": memory_id, "message": "Stored."}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Update
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def update(*, id: str, content: str | None = None, title: str | None = None,
           tags: list[str] | None = None, status: str | None = None,
           scope: str = "project") -> dict:
    """Partial update by ID. Content changes trigger re-embedding."""
    db = get_db(scope)
    row = db.execute("SELECT * FROM memories WHERE id = ?", (id,)).fetchone()
    if not row:
        return {"success": False, "message": f"Not found: {id}"}

    # Validate status if provided
    valid_statuses = ("active", "invalidated", "archived")
    if status is not None and status not in valid_statuses:
        return {"success": False, "message": f"Invalid status: {status}. Must be one of: {valid_statuses}"}

    now = time.time()
    new_content = _normalize(content) if content else row["content"]
    new_title = title.strip()[:200] if title else row["title"]
    new_tags = json.dumps(sorted(set(
        t.lower().strip() for t in tags if t.strip()
    ))) if tags is not None else row["tags"]
    new_hash = hashlib.sha256(new_content.encode()).hexdigest()
    new_status = status if status is not None else (row["status"] if "status" in row.keys() else "active")

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
           content_hash=?, embedded=?, updated_at=?, status=? WHERE id=?""",
        (new_title, new_content, new_tags, new_hash,
         0 if needs_reembed else row["embedded"], now, new_status, id),
    )

    if needs_reembed:
        db.execute("DELETE FROM memories_vec WHERE memory_id = ?", (id,))
        _try_embed(db, id, new_title, new_content)

    # M2 hysteresis: chunk/unchunk/re-chunk based on new content length.
    # Returns chunk-action indicator so M3a can enqueue accordingly.
    chunked_after = False
    if content is not None:
        chunked_after = _update_chunks_hysteresis(
            db, id, new_content, now,
        )

    # M3a (T5): enqueue worker tasks on content change.
    if content is not None:
        _enqueue_extract(db, id, new_content, now)
        if chunked_after:
            _enqueue_reembed(db, id, now)

    db.commit()

    result = {"success": True, "id": id, "message": "Updated."}
    if status is not None:
        result["status"] = new_status
    return result


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


def list_memories(*, scope: str = "project", tags: list[str] | None = None,
                  limit: int = 20, offset: int = 0,
                  include_archived: bool = False) -> dict:
    """Browse stored memories with optional tag filtering (AND logic).

    By default only shows active memories. Set include_archived=True
    to also see invalidated and archived memories.
    """
    db = get_db(scope)
    limit = max(1, min(limit, 100))

    where_parts: list[str] = []
    params: list = []

    if not include_archived:
        where_parts.append("status = 'active'")

    if tags:
        for tag in tags:
            where_parts.append("tags LIKE ?")
            params.append(f'%"{tag.lower().strip()}"%')

    where = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    total = db.execute(f"SELECT COUNT(*) c FROM memories{where}", params).fetchone()["c"]
    rows = db.execute(
        f"""SELECT id, title, tags, access_count, status, updated_at
            FROM memories{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
        [*params, limit, max(0, offset)],
    ).fetchall()

    return {
        "items": [
            {"id": r["id"], "title": r["title"],
             "tags": json.loads(r["tags"]), "access_count": r["access_count"],
             "status": r["status"] if "status" in r.keys() else "active"}
            for r in rows
        ],
        "total": total,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Embed pending (called from search)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def embed_pending(
    db, batch_size: int = 50, memory_id: str | None = None,
) -> int:
    """Embed memories whose vector is stale per memory_embedding_meta.

    M1 refactor (T12): the previous version filtered on `memories.embedded=0`
    only — missing the case where a legacy backfilled row (model='legacy',
    version='0') needs re-embedding under the current resolver. Now the
    staleness check is meta-aware:

      stale if NO meta row OR meta.(name, version, dim) ≠ active resolver

    M3a (T6a): optional `memory_id` filter — the worker uses this to
    embed exactly one memory's row when draining a `reembed` task.

    On success, writes:
      - memories_vec (replace)
      - memory_embedding_meta (INSERT OR REPLACE with active resolver fields)
      - memories.embedded = 1 (back-compat — preserved for existing tests
        and any consumer that still reads the flag)
    """
    embedder = get_embedder()
    if not _embedder_meets_threshold(embedder):
        return 0

    params = {
        "name": embedder.name,
        "version": embedder.version,
        "dim": embedder.dim,
        "batch": batch_size,
    }
    mem_filter = ""
    if memory_id is not None:
        mem_filter = " AND m.id = :mid"
        params["mid"] = memory_id

    # Stale-row query: LEFT JOIN + parenthesized OR-chain. The IS NULL
    # branch handles LEFT JOIN no-match before any `!=` would compare NULL.
    rows = db.execute(
        f"""
        SELECT m.id, m.title, m.content
          FROM memories m
          LEFT JOIN memory_embedding_meta em ON em.memory_id = m.id
         WHERE ((em.memory_id IS NULL)
            OR (em.dim != :dim)
            OR (em.model_name != :name)
            OR (em.model_version != :version)){mem_filter}
         LIMIT :batch
        """,
        params,
    ).fetchall()

    count = 0
    now_unix = time.time()
    for r in rows:
        try:
            vec = embedder.embed(f"{r['title']}. {r['content']}")
            db.execute(
                "INSERT OR REPLACE INTO memories_vec (memory_id, embedding) VALUES (?, ?)",
                (r["id"], serialize_vec(vec)),
            )
            db.execute(
                """
                INSERT OR REPLACE INTO memory_embedding_meta
                    (memory_id, model_name, model_version, dim, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (r["id"], embedder.name, embedder.version, embedder.dim, now_unix),
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
        if _embedder_meets_threshold(embedder):
            vec = embedder.embed(f"{title}. {content}")
            db.execute(
                "INSERT INTO memories_vec (memory_id, embedding) VALUES (?, ?)",
                (memory_id, serialize_vec(vec)),
            )
            db.execute(
                """INSERT OR REPLACE INTO memory_embedding_meta
                       (memory_id, model_name, model_version, dim, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (memory_id, embedder.name, embedder.version, embedder.dim, time.time()),
            )
            db.execute("UPDATE memories SET embedded = 1 WHERE id = ?", (memory_id,))
            db.commit()
    except Exception:
        pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Chunking (M2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _try_embed_chunk(db, chunk_id: str, content: str, *, embedder=None) -> bool:
    """Embed a single chunk and write chunks_vec + chunk_embedding_meta.
    Does NOT commit — caller batches commits. Returns True on success."""
    try:
        if embedder is None:
            embedder = get_embedder()
        if not _embedder_meets_threshold(embedder):
            return False
        vec = embedder.embed(content)
        db.execute(
            "INSERT OR REPLACE INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, serialize_vec(vec)),
        )
        db.execute(
            """INSERT OR REPLACE INTO chunk_embedding_meta
                   (chunk_id, model_name, model_version, dim, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (chunk_id, embedder.name, embedder.version, embedder.dim, time.time()),
        )
        return True
    except Exception:
        return False


def _chunk_and_embed(db, memory_id: str, content: str, now: float, *, force: bool = False) -> int:
    """If content > CHUNK_THRESHOLD (or `force=True`), split into chunks,
    INSERT chunk rows, and embed up to MAX_CHUNKS_PER_MEMORY chunks (per
    ADR-002). All chunks are stored as rows; only chunks_vec inserts are
    deferred past the cap. Returns number of chunks created. Commits once
    at the end if work was done. The `force` flag is used by the update
    hysteresis in-band re-chunk path."""
    chunks = chunker_split(content, force=force)
    if not chunks:
        return 0

    over_cap = max(0, len(chunks) - MAX_CHUNKS_PER_MEMORY)
    for idx, (chunk_text, byte_start, byte_end) in enumerate(chunks):
        chunk_id = uuid.uuid4().hex
        db.execute(
            """INSERT INTO chunks
                   (id, memory_id, chunk_index, content, byte_start, byte_end, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (chunk_id, memory_id, idx, chunk_text, byte_start, byte_end, now),
        )
        # M3a (T5): chunks_vec writes moved off the sync path. The
        # worker handles chunk embedding via reembed tasks enqueued
        # by store()/update().
    if over_cap:
        # Log a warning per ADR-002 §Failure Modes
        import logging
        logging.getLogger("sage_memory.store").warning(
            "memory %s produced %d chunks (cap=%d); %d chunks skipped vec embedding "
            "(deferred — chunks_fts coverage preserved).",
            memory_id, len(chunks), MAX_CHUNKS_PER_MEMORY, over_cap,
        )
    db.commit()
    return len(chunks)


def _update_chunks_hysteresis(
    db, memory_id: str, new_content: str, now: float,
) -> bool:
    """Bidirectional chunking hysteresis per ADR-002. Returns True
    iff the memory has chunks AFTER this call (caller uses this to
    decide whether to enqueue a reembed task in M3a).

    - chunked=False AND new_len > CHUNK_THRESHOLD → chunk on grow
    - chunked=True  AND new_len < HYSTERESIS_LOW  → unchunk on shrink
    - chunked=True  AND new_len in [HYSTERESIS_LOW, ∞) → re-chunk in place
    - chunked=False AND new_len <= CHUNK_THRESHOLD → no chunk action
    """
    chunked = db.execute(
        "SELECT 1 FROM chunks WHERE memory_id = ? LIMIT 1", (memory_id,)
    ).fetchone() is not None
    new_len = len(new_content)

    if chunked and new_len < HYSTERESIS_LOW:
        # Unchunk: drop all chunks (CASCADE removes chunks_vec + chunk_embedding_meta)
        db.execute("DELETE FROM chunks WHERE memory_id = ?", (memory_id,))
        return False

    if chunked:
        # Re-chunk in place: drop old, write new. `force=True` because
        # in-band content (1500 ≤ x < 2000) is below the natural chunk
        # threshold but the memory was already chunked — per ADR-002
        # §Hysteresis it stays chunked.
        db.execute("DELETE FROM chunks WHERE memory_id = ?", (memory_id,))
        n = _chunk_and_embed(db, memory_id, new_content, now, force=True)
        return n > 0

    if not chunked and new_len > CHUNK_THRESHOLD:
        # Chunk on grow
        n = _chunk_and_embed(db, memory_id, new_content, now)
        return n > 0

    # not chunked and not over threshold → no-op
    return False


def embed_pending_chunks(
    db, batch_size: int = 50, memory_id: str | None = None,
) -> int:
    """Embed chunks whose vector is stale per chunk_embedding_meta.

    Mirrors `embed_pending` for memories but operates on chunks. In M3a
    the worker is the sole caller (search no longer triggers embeds);
    the `memory_id` filter lets the worker embed exactly the chunks of
    the memory whose `reembed` task it just claimed.
    """
    embedder = get_embedder()
    if not _embedder_meets_threshold(embedder):
        return 0

    params = {
        "name": embedder.name,
        "version": embedder.version,
        "dim": embedder.dim,
        "batch": batch_size,
    }
    mem_filter = ""
    if memory_id is not None:
        mem_filter = " AND c.memory_id = :mid"
        params["mid"] = memory_id

    rows = db.execute(
        f"""
        SELECT c.id, c.content
          FROM chunks c
          LEFT JOIN chunk_embedding_meta cm ON cm.chunk_id = c.id
         WHERE ((cm.chunk_id IS NULL)
            OR (cm.dim != :dim)
            OR (cm.model_name != :name)
            OR (cm.model_version != :version)){mem_filter}
         LIMIT :batch
        """,
        params,
    ).fetchall()

    count = 0
    for r in rows:
        if _try_embed_chunk(db, r["id"], r["content"], embedder=embedder):
            count += 1
        else:
            break
    if count:
        db.commit()
    return count
