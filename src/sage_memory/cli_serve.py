"""M1.2 — `sage-memory serve` CLI subcommand.

Hand-rolled flag parsing matching the cli_dedup.py pattern. The CLI
maps user-facing transport names to FastMCP's internal vocabulary:

  stdio → stdio
  sse   → sse
  http  → streamable-http

Stdio is the default and matches the arg-less ``sage-memory``
backwards-compat path; non-stdio transports serve ``mcp.http_app()``
via our own ``uvicorn.run()`` (see the comment at the call site for
why ``FastMCP.run()`` is bypassed). See ADR-007 rev 2.
"""

from __future__ import annotations

import asyncio
import logging
import sys


logger = logging.getLogger("sage_memory.cli_serve")


# User-facing → FastMCP internal transport name.
_TRANSPORT_MAP = {
    "stdio": "stdio",
    "sse": "sse",
    "http": "streamable-http",
}

_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


_HELP_TEXT = """\
sage-memory serve — start the MCP server (0.13.0+)

Usage:
  sage-memory serve [--transport stdio|sse|http]
                    [--port 3333]
                    [--host 127.0.0.1]
                    [--hub]
                    [--log-level DEBUG|INFO|WARNING|ERROR|CRITICAL]

Flags:
  --transport   Transport protocol (default: stdio).
                  stdio  MCP over stdin/stdout (matches the arg-less
                         `sage-memory` invocation; backwards compat).
                  sse    MCP over Server-Sent Events; opens a TCP socket.
                  http   MCP over streamable HTTP; opens a TCP socket.
  --port        TCP port for sse/http (default: 3333; ignored for stdio).
  --host        TCP bind host for sse/http (default: 127.0.0.1).
                Set to 0.0.0.0 for remote exposure; reverse-proxy
                required (see docs/guides/self-hosted-server.md).
  --hub         Activate hub-aware MCP tool behavior (search + store
                gain hub_projects / hub_target params; M2/M3).
  --log-level   sage-memory + FastMCP logger threshold (default: INFO).

Examples:
  sage-memory                            # arg-less → serve --transport stdio
  sage-memory serve --transport sse --port 3333
  sage-memory serve --transport http --host 0.0.0.0
"""


class _Flags:
    transport: str = "stdio"
    port: int = 3333
    host: str = "127.0.0.1"
    hub: bool = False
    log_level: str = "INFO"


def _parse_flags(argv: list[str]) -> _Flags | None:
    flags = _Flags()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--transport":
            if i + 1 >= len(argv):
                print(
                    "sage-memory serve: --transport requires a value\n",
                    file=sys.stderr,
                )
                print(_HELP_TEXT, file=sys.stderr)
                return None
            value = argv[i + 1]
            if value not in _TRANSPORT_MAP:
                print(
                    f"sage-memory serve: --transport must be one of "
                    f"{sorted(_TRANSPORT_MAP)} (got {value!r})\n",
                    file=sys.stderr,
                )
                return None
            flags.transport = value
            i += 2
        elif a == "--port":
            if i + 1 >= len(argv):
                print(
                    "sage-memory serve: --port requires a value\n",
                    file=sys.stderr,
                )
                return None
            try:
                flags.port = int(argv[i + 1])
            except ValueError:
                print(
                    f"sage-memory serve: --port must be an integer "
                    f"(got {argv[i + 1]!r})\n",
                    file=sys.stderr,
                )
                return None
            i += 2
        elif a == "--host":
            if i + 1 >= len(argv):
                print(
                    "sage-memory serve: --host requires a value\n",
                    file=sys.stderr,
                )
                return None
            flags.host = argv[i + 1]
            i += 2
        elif a == "--hub":
            flags.hub = True
            i += 1
        elif a == "--log-level":
            if i + 1 >= len(argv):
                print(
                    "sage-memory serve: --log-level requires a value\n",
                    file=sys.stderr,
                )
                return None
            level = argv[i + 1].upper()
            if level not in _VALID_LOG_LEVELS:
                print(
                    f"sage-memory serve: --log-level must be one of "
                    f"{list(_VALID_LOG_LEVELS)} (got {argv[i + 1]!r})\n",
                    file=sys.stderr,
                )
                return None
            flags.log_level = level
            i += 2
        else:
            print(
                f"sage-memory serve: unknown flag: {a}\n",
                file=sys.stderr,
            )
            print(_HELP_TEXT, file=sys.stderr)
            return None
    return flags


def run_serve(argv: list[str]) -> int:
    """Entry point dispatched from ``__init__.py:main()``. Returns exit code."""
    if argv and argv[0] in ("-h", "--help"):
        print(_HELP_TEXT)
        return 0

    flags = _parse_flags(argv)
    if flags is None:
        return 2

    # --log-level applies to the sage-memory + FastMCP loggers; the
    # MCP request-handling path inherits from these.
    logging.basicConfig(level=getattr(logging, flags.log_level))

    from .server_fastmcp import build_mcp_app
    mcp = build_mcp_app(hub_enabled=flags.hub)

    if flags.transport == "stdio":
        # asyncio.run matches the arg-less __init__.py:main()
        # dispatch (asyncio.run(server.run())) so both paths share
        # the same event loop implementation. server_lifespan fires
        # automatically inside run_stdio_async via FastMCP's startup hook.
        asyncio.run(mcp.run_stdio_async())
        return 0

    # SSE / streamable-HTTP: serve the Starlette app with our own
    # uvicorn instead of FastMCP.run(). In FastMCP 3.4.5 the run() path
    # enters the user lifespan in the RUNNER task and the app's ASGI
    # lifespan only takes a reference — on SIGTERM uvicorn drains the
    # app but the runner's exit-stack close never completes, so the
    # lifespan finally block (worker stop, access-count flush, DB
    # close) silently never runs. Serving mcp.http_app() directly makes
    # the ASGI lifespan the owner, and its shutdown path runs the
    # finally block (verified: ASGI-level lifespan drive + SIGTERM
    # against uvicorn). Default endpoint paths match FastMCP 1.0:
    # /sse for sse, /mcp for streamable-http.
    import uvicorn

    app = mcp.http_app(transport=_TRANSPORT_MAP[flags.transport])
    uvicorn.run(
        app,
        host=flags.host,
        port=flags.port,
        log_level=flags.log_level.lower(),
    )
    return 0
