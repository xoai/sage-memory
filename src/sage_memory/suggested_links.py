"""Suggested-link discovery for `sage_memory_store` / `update`.

When the agent stores a memory, sage-memory does a fast FTS5 BM25
search to surface up to 3 existing memories whose titles/content
resemble the new one. The agent can then call `sage_memory_link` to
formalize the connection. No LLM call; no full `search()` pipeline.

Design constraints (per spec §`suggested_links.py`):
- Direct FTS5 query against the project DB; NO RRF / vector / graph /
  dual-DB merge.
- `status='active'` filter (invalidated entries don't surface).
- Reuses `search._build_fts_query` so tokenization is consistent
  with the headline 0.972 R@5 free-path bench. MUST NOT modify
  `_build_fts_query`.
- Cap at top-3 results. No BM25 threshold (would require corpus
  calibration); rely on LIMIT + the limit param.
"""

from __future__ import annotations

import re

from .search import _build_fts_query


_MIN_CONTENT_LEN = 20

# Cap the content passed to `_build_fts_query`. Beyond a few hundred
# tokens, the FTS5 OR query becomes pathologically slow for what is
# only meant to be a "suggested links" lookup. The first 2000 chars
# carry enough signal to find candidates; the title and lede of any
# bundled-skill body, knowledge entry, or learning fits well within
# that envelope.
_MAX_QUERY_CONTENT_LEN = 2000


def find_suggested_links(
    conn, content: str, *, limit: int = 3, exclude_id: str | None = None,
) -> list[dict]:
    """Return up to `limit` candidate link targets for `content`.

    Each entry: `{"target_id", "target_title", "reason"}`.
    Returns `[]` for short content or when no FTS query can be built.

    `exclude_id` filters out a specific memory (the one being stored
    or updated) so the agent never sees a self-suggestion.
    """
    if not content or len(content) < _MIN_CONTENT_LEN:
        return []
    # Truncate before FTS5 query building — see _MAX_QUERY_CONTENT_LEN
    # comment for rationale. Without this cap, a 30K-word content
    # produces a 30K-term OR query that hangs FTS5.
    query_input = content[:_MAX_QUERY_CONTENT_LEN]
    fts_query = _build_fts_query(conn, query_input)
    if not fts_query:
        return []
    # Over-fetch slightly so excluding the current memory still leaves
    # us with `limit` real candidates in the typical case.
    fetch = max(limit + 2, 5)
    rows = conn.execute(
        """
        SELECT m.id AS id, m.title AS title, bm25(memories_fts) AS score
        FROM memories_fts
        JOIN memories m ON m.rowid = memories_fts.rowid
        WHERE memories_fts MATCH ?
          AND m.status = 'active'
        ORDER BY score
        LIMIT ?
        """,
        (fts_query, fetch),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        if exclude_id is not None and row["id"] == exclude_id:
            continue
        out.append({
            "target_id": row["id"],
            "target_title": row["title"],
            "reason": _build_reason(content, row["title"]),
        })
        if len(out) >= limit:
            break
    return out


def _build_reason(content: str, title: str) -> str:
    """Compose a short 'reason' string showing which tokens overlap.

    Uses simple word-set intersection (lowercased, alnum-only). Falls
    back to a generic "content overlap" if no clean tokens overlap —
    BM25 saw a match even if our naïve tokenizer didn't.
    """
    content_tokens = _alnum_tokens(content.lower())
    title_tokens = _alnum_tokens(title.lower())
    common = [t for t in content_tokens if t in title_tokens]
    if common:
        snippet = " ".join(common[:3])
        title_prefix = title.strip()[:80]
        return f"name match: '{snippet}' → {title_prefix}"
    return f"content overlap → {title.strip()[:80]}"


def _alnum_tokens(text: str) -> list[str]:
    """Lowercase + alnum-only tokenization. Cheap; not FTS5-aware
    (that's _build_fts_query's job)."""
    return [t for t in re.split(r"[^a-z0-9]+", text) if len(t) > 2]
