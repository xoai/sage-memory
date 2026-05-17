"""Implementation of `sage-memory status` subcommand.

Prints, in a human-readable table:
- Active embedder (name, version, dim, quality, class)
- Corpus vec_dim (from corpus_meta)
- Stale-embedding count (rows where meta is missing OR dim != corpus
  OR model_name/model_version doesn't match the active embedder)
- Total memories count

The stale-count SQL uses a parenthesized OR-chain to make precedence
explicit. With LEFT JOIN, when no meta row exists, em.dim/em.model_name
are NULL and `!=` would yield NULL (falsy) — the `IS NULL` branch
correctly handles that case.
"""

from __future__ import annotations

from .db import get_db
from .embedder import get_embedder


STALE_SQL = """\
SELECT COUNT(*)
  FROM memories m
  LEFT JOIN memory_embedding_meta em ON em.memory_id = m.id
 WHERE (em.memory_id IS NULL)
    OR (em.dim != :corpus_dim)
    OR (em.model_name != :current_embedder_name)
    OR (em.model_version != :current_embedder_version)
"""


def print_status() -> None:
    db = get_db()  # project DB if available, else global
    embedder = get_embedder()

    # Corpus dim from corpus_meta (set by migration 005). May be NULL
    # only on DBs that haven't migrated past 004 yet.
    corpus_row = db.execute(
        "SELECT value FROM corpus_meta WHERE key = 'vec_dim'"
    ).fetchone()
    corpus_dim = int(corpus_row[0]) if corpus_row else None

    total = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    if corpus_dim is not None:
        stale = db.execute(
            STALE_SQL,
            {
                "corpus_dim": corpus_dim,
                "current_embedder_name": embedder.name,
                "current_embedder_version": embedder.version,
            },
        ).fetchone()[0]
    else:
        stale = "n/a (pre-005 schema)"

    print("sage-memory status")
    print("─" * 60)
    print(f"  Embedder:")
    print(f"    name:    {embedder.name}")
    print(f"    version: {embedder.version}")
    print(f"    dim:     {embedder.dim}")
    print(f"    quality: {embedder.quality}")
    print(f"    class:   {type(embedder).__name__}")
    print(f"  Corpus:")
    print(f"    vec_dim: {corpus_dim}")
    print(f"  Memories:")
    print(f"    total:   {total}")
    print(f"    stale:   {stale}")
