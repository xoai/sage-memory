"""FastMCP factory for sage-memory's MCP server.

M1.1a of the 0.13.0 team-MCP-transports cycle ported tool registration
from the low-level ``mcp.server.Server`` API to ``FastMCP``. The
existing ``server.py:run()`` is unchanged at this milestone — the
factory only PRODUCES a FastMCP instance that publishes byte-equal
``tools/list`` output against the pre-migration baseline.

Why hand-crafted ``parameters`` matter: sage-memory's tool schemas are
hand-crafted (descriptions, enums, defaults, ordering) and have been
treated as the contract by every MCP client integration since 0.5.x.
FastMCP v3's ``FunctionTool`` accepts a ``parameters`` dict that is
published verbatim, and a handler with a ``(**kwargs)`` signature
receives incoming arguments unvalidated — matching the dispatch shape
used by the low-level server (``handler(**(arguments or {}))``). (On
FastMCP 1.0 this required a ``PassthroughFuncMetadata`` subclass and a
private ``_tool_manager._tools`` insertion; v3 makes both unnecessary.)

See ADR-007 rev 2 for the migration rationale and the FastMCP
bail-out decision rule that governs introspection-diff failures, and
decisions.md 2026-07-29 for the FastMCP 3.x vs mcp 2.x direction call.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from mcp.types import TextContent

from . import db as _db
from . import llm
from . import search as _search
from .embedder import (
    DimMismatchRefuseError, LocalEmbedder, resolve, set_embedder,
)
from .worker import Worker

logger = logging.getLogger("sage-memory")


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _host_without_port(host_header: str) -> str:
    """Strip the port from a Host header, IPv6-bracket aware."""
    h = host_header.strip()
    if h.startswith("["):  # [::1]:3333
        return h[1:h.index("]")] if "]" in h else h
    if h.count(":") == 1:  # example.com:3333
        return h.rsplit(":", 1)[0]
    return h  # bare IPv6 or hostname without port


class SecurityMiddleware:
    """Pure-ASGI bearer-auth + Host-allowlist + Origin gate (P0-3).

    Order matters: Host first (cheap, defeats DNS rebinding before any
    auth work), then Origin (browser cross-origin defense), then the
    bearer token (the real gate for non-browser clients — a non-browser
    client controls its own Host header, per the sibling project's
    hardening retrospective).

    - Host: must be a loopback spelling or an explicit allowed host
      → else 403. Compared with the port stripped.
    - Origin: when present, its host must satisfy the same rule
      → else 403. Absent Origin = non-browser client; allowed.
    - Authorization: only enforced when a token is configured;
      ``hmac.compare_digest`` against ``Bearer <token>`` → else 401.

    stdio never sees this middleware (no HTTP surface). Loopback
    binds without a token pass straight through (zero-config local
    use, invariant 9).
    """

    def __init__(
        self,
        app: Any,
        *,
        token: str | None = None,
        allowed_hosts: list[str] | tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self._token = token
        self._allowed = _LOOPBACK_HOSTS | set(allowed_hosts)

    def _host_ok(self, host_header: str) -> bool:
        return _host_without_port(host_header) in self._allowed

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }

        host = headers.get("host", "")
        if not host or not self._host_ok(host):
            await self._reject(send, 403, "forbidden host")
            return

        origin = headers.get("origin")
        if origin is not None:
            from urllib.parse import urlsplit
            origin_host = urlsplit(origin).hostname or ""
            if origin_host not in self._allowed:
                await self._reject(send, 403, "forbidden origin")
                return

        if self._token is not None:
            import hmac
            expected = f"Bearer {self._token}"
            provided = headers.get("authorization", "")
            if not hmac.compare_digest(provided, expected):
                await self._reject(
                    send, 401, "unauthorized",
                    extra_headers=[(b"www-authenticate", b"Bearer")],
                )
                return

        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send, status: int, message: str,
                      extra_headers: list | None = None) -> None:
        body = json.dumps({"error": message}).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ] + list(extra_headers or [])
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        })
        await send({"type": "http.response.body", "body": body})


# Tools whose envelopes carried `_project` (current project name) in
# the pre-M1.1b dispatch path (server.py:572-577 of 0.12.x). Existing
# MCP clients have consumed this since 0.5.x — dropping it silently
# would regress the backwards-compat contract.
_ENRICHMENT_TOOLS = frozenset({
    "sage_memory_set_project",
    "sage_memory_store",
    "sage_memory_search",
    "sage_memory_list",
})


def _shutdown_trace(stage: str) -> None:
    """Write a shutdown-stage marker if SAGE_SHUTDOWN_TRACE is set.

    Used only by M1.7's graceful-shutdown test to verify the lifespan
    finally block runs to completion under SIGTERM. Silent + safe in
    production (env var unset → no-op).
    """
    import os
    path = os.environ.get("SAGE_SHUTDOWN_TRACE")
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(stage + "\n")
    except OSError:
        # Test observability must never destabilize production cleanup.
        pass


def _build_passthrough_tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    fn: Callable[..., Any],
) -> FunctionTool:
    """Build a ``FunctionTool`` whose published schema is the hand-crafted one.

    FastMCP v3 publishes ``parameters`` verbatim in tools/list, and the
    wrapped handler's ``(**kwargs)`` signature receives incoming arguments
    without Pydantic-derived validation — the passthrough semantics the
    pre-v3 ``PassthroughFuncMetadata`` hack existed to force.
    """
    return FunctionTool(
        fn=fn,
        name=name,
        description=description,
        parameters=parameters,
    )


def _wrap_handler_for_dispatch(
    name: str,
    handler: Callable[..., Any],
    *,
    hub_enabled: bool = False,
) -> Callable[..., Any]:
    """Wrap a sage-memory handler to preserve the pre-M1.1b envelope shape.

    The pre-migration low-level dispatch (server.py:563-583 of 0.12.x):

        async def _call(name, arguments):
            try:
                result = handler(**(arguments or {}))
                # _project enrichment for store/search/list/set_project
                if name in (...):
                    project = get_project_name()
                    if project:
                        result["_project"] = project
                return [TextContent(text=json.dumps(result, indent=2))]
            except Exception as e:
                logger.exception("Tool error: %s", name)
                return [TextContent(text=json.dumps({"error": str(e)}))]

    FastMCP's default dispatch produces a structurally different
    envelope: it routes raised exceptions through ``ToolError`` into
    a ``CallToolResult(isError=True)``, and serializes dict returns
    via ``pydantic_core.to_json`` instead of ``json.dumps`` (different
    Unicode/datetime handling). Both are wire-shape changes invisible
    to ``tools/list`` introspection but visible to every MCP client
    consuming sage-memory responses.

    This wrapper restores the exact pre-migration shape: ``_project``
    enriched, exceptions caught into ``{"error": str(e)}``, and the
    return value is a pre-built ``[TextContent]`` list (which FastMCP's
    ``_convert_to_content`` passes through verbatim, bypassing
    pydantic_core's serializer).

    When ``hub_enabled`` is True (server launched with ``--hub``),
    ``sage_memory_search`` recognises an optional ``hub_projects``
    kwarg and routes to ``hub.search.fan_out_search`` instead of the
    per-project ``search.search``. With ``hub_enabled`` False, the
    ``hub_projects`` kwarg is silently dropped before dispatch (a
    spec-compliant no-op per ADR-008 §"MCP integration").
    """
    async def _wrapped(**kwargs: Any) -> list[TextContent]:
        try:
            if name == "sage_memory_search":
                hub_projects = kwargs.pop("hub_projects", None)
                if hub_enabled and hub_projects is not None:
                    # M2 review M3: schema declares list[str] but
                    # FastMCP v3's TypeAdapter over the handler's
                    # (**kwargs) signature is a passthrough — no
                    # Pydantic validation of argument types.
                    # Type-check here so a malformed payload returns a
                    # clear error envelope rather than the confusing
                    # set-of-characters ValueError fan_out_search would
                    # surface from `set("ops")` if hub_projects is a
                    # bare string.
                    if not (
                        isinstance(hub_projects, list)
                        and all(isinstance(p, str) for p in hub_projects)
                    ):
                        raise ValueError(
                            "hub_projects must be a list of strings "
                            f"(got {type(hub_projects).__name__})"
                        )
                    from .hub import search as _hub_search
                    result = _hub_search.fan_out_search(
                        query=kwargs.get("query", ""),
                        hub_projects=hub_projects,
                        limit=kwargs.get("limit", 5),
                        tags=kwargs.get("tags"),
                    )
                else:
                    result = handler(**kwargs)
            elif name == "sage_memory_store":
                # M3.6 — hub_target routes the write to a named writable
                # hub project. Without --hub, the param is silently
                # dropped (same semantics as M2.5 hub_projects).
                hub_target = kwargs.pop("hub_target", None)
                if hub_enabled and hub_target is not None:
                    if not isinstance(hub_target, str) or not hub_target:
                        raise ValueError(
                            "hub_target must be a non-empty string "
                            f"(got {type(hub_target).__name__})"
                        )
                    from .hub.store import store_to_project
                    result = store_to_project(
                        hub_target,
                        content=kwargs.get("content", ""),
                        title=kwargs.get("title"),
                        tags=kwargs.get("tags"),
                        entities=kwargs.get("entities"),
                        relations=kwargs.get("relations"),
                    )
                else:
                    result = handler(**kwargs)
            else:
                result = handler(**kwargs)

            if name in _ENRICHMENT_TOOLS and isinstance(result, dict):
                project = _db.get_project_name()
                if project:
                    result["_project"] = project
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        except Exception as e:
            logger.exception("Tool error: %s", name)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    _wrapped.__name__ = f"_dispatch_{name}"
    return _wrapped


def _augment_for_hub(
    tool_def: Any,
    hub_enabled: bool,
) -> tuple[str, dict[str, Any]]:
    """Return ``(description, parameters)`` for a tool, conditionally
    extended with hub-aware text + schema fields when ``hub_enabled``.

    Currently augmented:
      - ``sage_memory_search`` gains ``hub_projects`` (M2.5)
      - ``sage_memory_store`` gains ``hub_target`` (M3.6)
    """
    description = tool_def.description or ""
    parameters = dict(tool_def.inputSchema or {})

    if not hub_enabled:
        return description, parameters

    if tool_def.name == "sage_memory_search":
        description = description + (
            "\n\n0.13.0 hub mode (server launched with --hub): pass "
            "`hub_projects: [\"name1\", \"name2\"]` to fan out the search "
            "across hub-registered projects (+ global). Results carry a "
            "`source` field set to the hub project name. Without "
            "`hub_projects`, behavior is unchanged (per-project search)."
        )
        properties = dict(parameters.get("properties") or {})
        properties["hub_projects"] = {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Hub-registered project names to fan out across. Names "
                "must match `searchable: true` entries in ~/.sage-hub.yaml. "
                "Ignored when the server is not in --hub mode."
            ),
        }
        parameters["properties"] = properties
        return description, parameters

    if tool_def.name == "sage_memory_store":
        description = description + (
            "\n\n0.13.0 hub mode (server launched with --hub): pass "
            "`hub_target: \"name\"` to route the write to a writable "
            "hub-registered project. The write lands in that project's "
            "DB, not the active project. Without `hub_target`, behavior "
            "is unchanged."
        )
        properties = dict(parameters.get("properties") or {})
        properties["hub_target"] = {
            "type": "string",
            "description": (
                "Hub-registered project name to route the write to. "
                "Must match a `writable: true` entry in ~/.sage-hub.yaml. "
                "Ignored when the server is not in --hub mode."
            ),
        }
        parameters["properties"] = properties
        return description, parameters

    return description, parameters


@asynccontextmanager
async def server_lifespan(_: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Embedder bootstrap + worker probe → yield → flush + close.

    Mirrors the pre-M1.1b ``server.py:run()`` bootstrap/teardown
    sequence exactly so backwards-compat is preserved (M1.1a's
    byte-equal contract extends to runtime behavior here). The
    ``DimMismatchRefuseError`` path re-raises so the operator sees the
    failure and runs ``sage-memory reindex``; other embedder bootstrap
    failures fall back to ``LocalEmbedder`` per the original.

    Module-level access (``_db.close_all`` / ``_search.flush_all_access``)
    is intentional: it keeps ``monkeypatch.setattr(db, ...)`` /
    ``monkeypatch.setattr(search, ...)`` effective in tests.
    """
    # Lazy import: server.py exports _resolve_db_path + _needs_worker,
    # and imports from .store / .search / .graph / .db. Pulling them at
    # module load would build a circular import via the factory's
    # ``from . import server`` in build_mcp_app.
    from .server import _needs_worker, _resolve_db_path

    worker: Worker | None = None

    # --- embedder bootstrap ---
    try:
        conn = _db.get_db()
        row = conn.execute(
            "SELECT value FROM corpus_meta WHERE key = 'vec_dim'"
        ).fetchone()
        corpus_dim = int(row["value"]) if row else 384
        embedder = resolve(corpus_dim)
        set_embedder(embedder)
        logger.info(
            "embedder: %s active (dim=%d, quality=%.2f)",
            type(embedder).__name__, embedder.dim, embedder.quality,
        )
    except DimMismatchRefuseError as e:
        logger.error(
            "embedder: refusing to start — %s. "
            "Run: sage-memory reindex --re-embed --embedder <name>",
            e,
        )
        raise
    except Exception:
        logger.exception(
            "embedder: resolver bootstrap failed; falling back to "
            "LocalEmbedder (384d, quality 0.45)"
        )
        set_embedder(LocalEmbedder())

    # --- worker probe ---
    try:
        db_path = _resolve_db_path()
        if db_path:
            conn = _db.get_db()
            if _needs_worker(conn):
                worker = Worker(db_path)
                worker.start()
    except Exception:
        logger.exception(
            "worker: startup probe failed; continuing without worker"
        )
        worker = None

    try:
        yield {}
    finally:
        # Cleanup ordering matches the pre-M1.1b run() finally block.
        # The shutdown-trace file (SAGE_SHUTDOWN_TRACE env var) is the
        # observability hook M1.7's graceful-shutdown test relies on —
        # uvicorn's logging config swallows sage-memory's logger output
        # when stdout/stderr are piped, so a file-based marker is the
        # most reliable signal that the lifespan finally block ran.
        if worker is not None:
            worker.stop()
            _shutdown_trace("worker.stop")
        _search.flush_all_access()
        _shutdown_trace("flush_all_access")
        _db.close_all()
        _shutdown_trace("close_all")


def build_mcp_app(
    hub_enabled: bool = False,
) -> FastMCP:
    """Construct a FastMCP app with sage-memory's tools + lifespan.

    M1.1b wires ``server_lifespan`` so embedder bootstrap, worker
    probe, and cleanup fire automatically around the MCP server's
    request loop. Transport settings (host/port) are NOT accepted here:
    FastMCP v3 keeps the server definition transport-independent —
    callers pass them to ``run()``/``run_async()`` instead.
    """
    # Imported lazily to avoid a circular import: server.py imports from
    # the handler modules (.store, .search, .graph, .db) and exports
    # the TOOLS list + HANDLERS dict that this factory consumes.
    from . import server as srv

    mcp = FastMCP(
        name="sage-memory",
        lifespan=server_lifespan,
    )

    for tool_def in srv.TOOLS:
        handler = srv.HANDLERS[tool_def.name]
        wrapped = _wrap_handler_for_dispatch(
            tool_def.name, handler, hub_enabled=hub_enabled,
        )
        description, parameters = _augment_for_hub(tool_def, hub_enabled)
        tool = _build_passthrough_tool(
            name=tool_def.name,
            description=description,
            parameters=parameters,
            fn=wrapped,
        )
        mcp.add_tool(tool)

    # /health endpoint (M1.6). Active only on SSE / HTTP transports —
    # stdio has no HTTP route surface. The route returning 200 IS the
    # health signal: if the process can serve the response, the
    # FastMCP factory + lifespan have started cleanly. spec.md §"/health
    # endpoint" reserves `?deep=1` for a future DB-connectivity check.
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def _health(_: Request) -> JSONResponse:
        from . import __version__
        return JSONResponse({"status": "ok", "version": __version__})

    return mcp
