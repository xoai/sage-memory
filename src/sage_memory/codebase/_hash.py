"""Content-hash primitives for the codebase scanner.

Two distinct hashes — same algorithm (sha256), different inputs:

- :func:`file_content_hash` (unsalted) hashes raw bytes. It drives
  delta-rescan via ``codebase_scans.content_hash``: if the next scan
  computes the same value, the file is unchanged and parsing can be
  skipped.

- :func:`memory_content_hash` (path-salted) hashes a prefix-tagged
  payload. It is stored in ``memories.content_hash``, which has a
  UNIQUE constraint from migration 001. The salt prevents collisions
  between identical files at different paths (canonical case: dozens
  of empty ``__init__.py`` files in a Python package tree).

Per spec §"Note on content_hash semantics": the two values are
intentionally not comparable. They live in different tables and answer
different questions.
"""

from __future__ import annotations

import hashlib


def file_content_hash(data: bytes) -> str:
    """SHA-256 hex digest of raw file bytes (unsalted).

    Used as ``codebase_scans.content_hash`` for delta detection.
    """
    return hashlib.sha256(data).hexdigest()


def memory_content_hash(rel_path: str, data: bytes) -> str:
    """Path-salted SHA-256 hex digest for ``memories.content_hash``.

    Format (spec line 145): ``sha256(f"file:{rel_path}:{file_bytes}")``.
    The path is encoded to UTF-8 before concatenation with the raw
    bytes so binary content can also be hashed safely.
    """
    prefix = b"file:" + rel_path.encode("utf-8") + b":"
    return hashlib.sha256(prefix + data).hexdigest()
