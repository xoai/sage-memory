"""P2-2 — `sage-memory embedder use <name>` one-command upgrade path.

Validates the requested tier is usable, then delegates the
dimension-migration to the existing, tested
``cli_reindex._do_full_reembed`` (backup + recreate + corpus_meta
update + re-embed enqueue). No new migration logic lives here, and
nothing on this path touches the zero-dep floor: ``fastembed`` is
imported only inside the validate step (design brief §3).
"""

from __future__ import annotations

import os
import sys


_TIERS = ("fastembed", "openai", "voyage", "cohere", "local")

_TIER_KEYS = {
    "openai": "OPENAI_API_KEY",
    "voyage": "VOYAGE_API_KEY",
    "cohere": "COHERE_API_KEY",
}

_HELP_TEXT = """\
sage-memory embedder — one-command embedder upgrade path

Usage:
  sage-memory embedder use <name>

Tiers:
  fastembed  Local neural (bge-small, 384d) — needs the [neural] extra:
             pip install 'sage-memory[neural]'
  openai     text-embedding-3-small (1536d) — needs OPENAI_API_KEY
  voyage     voyage-3-lite (512d) — needs VOYAGE_API_KEY
  cohere     embed-english-v3.0 (1024d) — needs COHERE_API_KEY
  local      TF-IDF floor (384d, zero deps) — the downgrade path

What it does: validates the tier, then runs the same backup + recreate
+ re-embed flow as `sage-memory reindex --re-embed --embedder <name>`.
"""


def _validate_fastembed() -> str | None:
    """Return an error message when the [neural] extra is missing."""
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return (
            "the [neural] extra is required for fastembed. Install with:\n"
            "  pip install 'sage-memory[neural]'\n"
            "  (uvx: use 'sage-memory[neural]' as the package spec)"
        )
    return None


def run_embedder(argv: list[str]) -> int:
    """Entry point dispatched from ``__init__.py:main()``."""
    if not argv or argv[0] in ("-h", "--help"):
        print(_HELP_TEXT)
        return 0

    if argv[0] != "use" or len(argv) < 2:
        print(_HELP_TEXT, file=sys.stderr)
        return 2

    name = argv[1].strip().lower()
    if name not in _TIERS:
        print(
            f"sage-memory embedder: unknown tier {name!r} "
            f"(choose from {', '.join(_TIERS)})\n",
            file=sys.stderr,
        )
        return 2

    if name == "fastembed":
        err = _validate_fastembed()
        if err is not None:
            print(f"sage-memory embedder: {err}", file=sys.stderr)
            return 2
    elif name in _TIER_KEYS:
        key = _TIER_KEYS[name]
        if not os.environ.get(key):
            print(
                f"sage-memory embedder: {name} requires {key} in the "
                f"environment (never paste keys as arguments).",
                file=sys.stderr,
            )
            return 2

    # Dim-migration via the existing tested path (design brief §2).
    from .cli_reindex import _do_full_reembed
    rc = _do_full_reembed(embedder_name=name)
    if rc == 0:
        print(
            f"\nembedder: switched to {name}. Restart the MCP server "
            f"so the resolver picks it up; run `sage-memory status` "
            f"to watch stale counts drain."
        )
    return rc
