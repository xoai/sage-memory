"""Regression for /review M1 CRITICAL #2 + MAJOR M1.

The pre-M1.1b dispatch (server.py:563-583 of 0.12.x) had three load-
bearing properties that FastMCP's default dispatch path silently
breaks. ``server_fastmcp._wrap_handler_for_dispatch`` restores them:

  1. Handler exceptions are caught and returned as a SUCCESS
     ``CallToolResult`` with ``{"error": str(e)}`` payload — NOT
     raised through ``ToolError`` into ``isError=True``.
  2. Dict envelopes are serialized via ``json.dumps(result, indent=2)``,
     NOT ``pydantic_core.to_json`` (which differs on Unicode escaping,
     datetime handling, float formatting).
  3. ``_project`` is enriched for store / search / list / set_project
     envelopes when a project is active.

Tested in-process against the wrapper directly so the contract is
pinned at the unit level (subprocess end-to-end coverage of #3 lives
in tests/test_serve_stdio.py::test_stdio_envelope_includes_project_enrichment).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from mcp.types import TextContent

from sage_memory.server_fastmcp import _wrap_handler_for_dispatch


def _run(coro):
    return asyncio.run(coro)


def test_wrapper_catches_exceptions_into_error_envelope():
    """Handler that raises → wrapper returns [TextContent] with
    ``{"error": "<message>"}`` payload. No exception escapes."""

    def _raising_handler(**kwargs):
        raise RuntimeError("boom")

    wrapped = _wrap_handler_for_dispatch("test_tool", _raising_handler)
    result = _run(wrapped())

    assert isinstance(result, list) and len(result) == 1
    assert isinstance(result[0], TextContent)
    payload = json.loads(result[0].text)
    assert payload == {"error": "boom"}, (
        f"error envelope shape changed; got {payload!r}"
    )


def test_wrapper_uses_json_dumps_not_pydantic_core():
    """Unicode characters and indent=2 spacing match json.dumps verbatim.
    pydantic_core.to_json would emit different bytes on non-ASCII."""

    def _handler(**kwargs):
        # Em-dash + non-ASCII content stresses the Unicode-escape
        # divergence between json.dumps (default ensure_ascii=True)
        # and pydantic_core.to_json (no escape).
        return {"message": "Stored — café", "n": 1}

    wrapped = _wrap_handler_for_dispatch("any_tool", _handler)
    result = _run(wrapped())

    text = result[0].text
    expected = json.dumps({"message": "Stored — café", "n": 1}, indent=2)
    assert text == expected, (
        f"envelope text drift from json.dumps:\n"
        f"  got: {text!r}\n  expected: {expected!r}"
    )


def test_wrapper_enriches_project_for_listed_tools():
    """sage_memory_store / search / list / set_project all carry
    ``_project`` after wrap when get_project_name() returns a value."""

    def _handler(**kwargs):
        return {"success": True, "id": "abc123"}

    for name in (
        "sage_memory_store", "sage_memory_search",
        "sage_memory_list", "sage_memory_set_project",
    ):
        with patch(
            "sage_memory.server_fastmcp._db.get_project_name",
            return_value="my-test-project",
        ):
            wrapped = _wrap_handler_for_dispatch(name, _handler)
            payload = json.loads(_run(wrapped())[0].text)
        assert payload.get("_project") == "my-test-project", (
            f"{name} envelope missing _project enrichment: {payload!r}"
        )


def test_wrapper_skips_project_enrichment_for_unlisted_tools():
    """Tools NOT in the enrichment set must NOT gain a ``_project`` key
    — preserves pre-migration behavior of leaving link/graph/delete/
    update/scan-codebase envelopes alone."""

    def _handler(**kwargs):
        return {"success": True}

    for name in (
        "sage_memory_delete", "sage_memory_update", "sage_memory_link",
        "sage_memory_graph", "sage_memory_scan_codebase",
    ):
        with patch(
            "sage_memory.server_fastmcp._db.get_project_name",
            return_value="my-test-project",
        ):
            wrapped = _wrap_handler_for_dispatch(name, _handler)
            payload = json.loads(_run(wrapped())[0].text)
        assert "_project" not in payload, (
            f"{name} envelope must not be enriched: {payload!r}"
        )


def test_wrapper_skips_project_enrichment_when_no_project_active():
    """If get_project_name() returns None/empty (no project active),
    no _project key is added — pre-migration guard at server.py:576
    (``if project:``)."""

    def _handler(**kwargs):
        return {"success": True}

    with patch(
        "sage_memory.server_fastmcp._db.get_project_name",
        return_value=None,
    ):
        wrapped = _wrap_handler_for_dispatch("sage_memory_store", _handler)
        payload = json.loads(_run(wrapped())[0].text)

    assert "_project" not in payload, (
        f"no project active → no _project key; got {payload!r}"
    )
