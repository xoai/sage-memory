# ADR-007 — MCP server transports (FastMCP)

**Status:** Accepted (rev 2); amended 2026-07-29 (FastMCP 3.x
direction + uvicorn serving, see decisions.md)
**Cited by:** `server_fastmcp.py`, `cli_serve.py`

## Context

Pre-0.13.0 the MCP server spoke stdio only via the low-level
`mcp.server.Server` API. Team setups need shared network transports
(SSE, streamable-HTTP). Every MCP client integration since 0.5.x
treats the hand-crafted tool schemas and response envelopes as the
contract — a framework migration must not change a single byte.

## Decision

- **Transports**: user-facing `stdio | sse | http` mapping to FastMCP
  `stdio | sse | streamable-http`. stdio is the default and matches
  the arg-less `sage-memory` invocation (backwards compat). Defaults:
  host 127.0.0.1, port 3333; endpoints `/sse` and `/mcp`.
- **Migration contract**: byte-equal `tools/list` against the
  pre-migration baseline (pinned by `tests/baselines/
  tools_list_no_hub.json`). Hand-crafted `parameters` dicts are
  published verbatim; dispatch matches
  `handler(**(arguments or {}))`; response envelopes preserve the
  `_project` enrichment, `{"error": str(e)}` exception shape, and
  `[TextContent]` passthrough — FastMCP's default dispatch
  (ToolError envelopes, pydantic_core serialization) is bypassed by
  a shared wrapper.
- **Bail-out rule (rev 2):** introspection-diff failures against the
  baseline are release-blockers, not warnings.
- **Amendment 2026-07-29:** the server runs on standalone FastMCP 3.x
  (mcp 2.0 removed `mcp.server.fastmcp`; see decisions.md for the
  FastMCP 3.x vs mcp 2.x analysis). For sse/http we serve
  `mcp.http_app()` under our own `uvicorn.run()` — FastMCP 3.4.5's
  `run()` never executes the user lifespan's finally block on
  SIGTERM (runner-task ownership bug), which would skip worker stop,
  access-count flush, and DB close.
- **Security (P0-3):** non-loopback sse/http binds refuse to start
  without a bearer token (`--token` / `SAGE_MEMORY_TOKEN`); Host
  allowlist + Origin validation middleware; stdio and loopback stay
  zero-config.

## Consequences

- One dispatch path for all three transports; tests pin the wire
  shape rather than the framework.
- The 2026-07-29 wire delta (`_meta.fastmcp.tags` on tools/list,
  stamped unconditionally by FastMCP v3) was accepted as
  spec-compliant and baseline-regenerated.

## Gaps

The original rev-1 alternatives analysis (why FastMCP over raw
starlette) is not recorded in code.
