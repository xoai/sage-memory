"""sage-memory — Ultrafast local MCP memory for LLMs.

Entry point dispatch:
- `sage-memory`         → run MCP server (back-compat, no args)
- `sage-memory run`     → explicit alias for the above
- `sage-memory status`  → print active embedder + corpus dim + stale count
"""

import asyncio
import sys
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from .server import run


try:
    __version__ = _pkg_version("sage-memory")
except PackageNotFoundError:
    # Source checkout without installed metadata — fall back to a
    # placeholder so the marker-block version line stays well-formed.
    __version__ = "0.0.0+unknown"


def main():
    """argparse-free dispatch — keeps the no-args path identical to the
    pre-M1 behavior so existing MCP launchers (Claude clients, etc.)
    continue to work without configuration changes."""
    argv = sys.argv[1:]

    # No-args OR explicit `run` → start MCP server (back-compat).
    if not argv or argv[0] == "run":
        asyncio.run(run())
        return

    if argv[0] == "status":
        from .cli_status import print_status
        print_status()
        return

    if argv[0] == "reindex":
        from .cli_reindex import run_reindex
        sys.exit(run_reindex(argv[1:]))

    if argv[0] == "dedup":
        from .cli_dedup import run_dedup
        sys.exit(run_dedup(argv[1:]))

    if argv[0] == "queue":
        from .cli_queue import run_queue
        sys.exit(run_queue(argv[1:]))

    if argv[0] == "install-skills":
        from .cli_install_skills import run_install_skills
        sys.exit(run_install_skills(argv[1:]))

    if argv[0] == "worker":
        # `worker --status` is the only flag in M3a; `worker --help`
        # prints the worker subcommand's help; bare `worker` shows usage.
        sub = argv[1] if len(argv) > 1 else ""
        if sub in ("-h", "--help") or sub == "":
            print(_WORKER_HELP_TEXT)
            return
        if sub == "--status":
            from .cli_worker import print_worker_status
            print_worker_status()
            return
        print(f"sage-memory worker: unknown flag: {sub}\n",
              file=sys.stderr)
        print(_WORKER_HELP_TEXT, file=sys.stderr)
        sys.exit(2)

    if argv[0] in ("-h", "--help"):
        print(_HELP_TEXT)
        return

    # Unknown subcommand — print help and exit non-zero.
    print(f"sage-memory: unknown subcommand: {argv[0]}\n", file=sys.stderr)
    print(_HELP_TEXT, file=sys.stderr)
    sys.exit(2)


_HELP_TEXT = """\
sage-memory — Ultrafast local MCP memory for LLMs

Usage:
  sage-memory                 Start the MCP server (default)
  sage-memory run             Same as above (explicit)
  sage-memory status          Show active embedder + corpus dim + stale count
  sage-memory worker --status Show background-worker queue depth
  sage-memory reindex --help  Re-embed memories + chunks (full or partial)
  sage-memory install-skills --help
                              Install built-in skills into AI agents
  sage-memory --help          Show this help

The MCP server speaks stdio; launch it from your MCP client config.
The status subcommand reads the local project DB and prints a summary
of the embedder configuration and any stale embeddings that need
re-indexing.
"""

_WORKER_HELP_TEXT = """\
sage-memory worker — background-worker diagnostics

Usage:
  sage-memory worker --status   Show queue depth by status + task_type
  sage-memory worker --help     Show this help

The worker drains the extraction_queue: LLM-driven entity/relation
extraction and async embedding (M3a). Status output reads the local
project DB; it does not communicate with a running worker.
"""
