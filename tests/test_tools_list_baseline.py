"""Byte-equal regression for ``build_mcp_app(hub_enabled=False)``.

M1.1a's load-bearing contract — "FastMCP-dispatched tools/list output
matches the pre-migration low-level Server output verbatim" — was
verified by an off-tree script (decisions.md 2026-05-24 M1.1a entry)
but had no automated regression test. M2's review surfaced this as
MAJOR #5: a future schema change to ``_augment_for_hub`` or
``server.TOOLS`` could silently break the contract without any test
firing.

This test pins the canonical baseline at
``tests/baselines/tools_list_no_hub.json``. Regenerate the baseline
when an intentional schema change lands:

    rm tests/baselines/tools_list_no_hub.json
    pytest tests/test_tools_list_baseline.py::test_capture_baseline  -p no:randomly

(or hand-run the snippet in this module's ``__main__`` block.)
"""

from __future__ import annotations

import asyncio
import difflib
import json
import sys
from pathlib import Path

from sage_memory.server_fastmcp import build_mcp_app


_BASELINE_PATH = (
    Path(__file__).parent / "baselines" / "tools_list_no_hub.json"
)


def _current_tools_json() -> str:
    async def _scenario():
        mcp = build_mcp_app(hub_enabled=False)
        tools = await mcp.list_tools()
        # FastMCP v3 list_tools() returns FunctionTool objects;
        # to_mcp_tool() converts to the mcp.types.Tool wire shape.
        return [
            json.loads(t.to_mcp_tool().model_dump_json(
                exclude_none=True, by_alias=True,
            ))
            for t in tools
        ]

    payload = asyncio.run(_scenario())
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def test_tools_list_no_hub_matches_baseline():
    """``build_mcp_app(hub_enabled=False).list_tools()`` must produce
    output byte-equal to ``tests/baselines/tools_list_no_hub.json``.

    Failure here means the wire-shape contract for default-mode MCP
    clients has changed — likely accidentally. Re-run capture only
    after confirming the change is intentional and updating the
    CHANGELOG."""
    assert _BASELINE_PATH.exists(), (
        f"baseline missing at {_BASELINE_PATH}; regenerate per "
        f"module docstring"
    )
    baseline = _BASELINE_PATH.read_text()
    current = _current_tools_json()
    if current == baseline:
        return

    diff = "".join(difflib.unified_diff(
        baseline.splitlines(keepends=True),
        current.splitlines(keepends=True),
        fromfile=str(_BASELINE_PATH),
        tofile="current build_mcp_app(hub_enabled=False) output",
        n=5,
    ))
    raise AssertionError(
        "tools/list output drifted from baseline. If the change is "
        "intentional, regenerate the baseline (see module docstring) "
        "and update CHANGELOG. Diff:\n\n" + diff
    )


if __name__ == "__main__":  # pragma: no cover — manual baseline capture
    _BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _BASELINE_PATH.write_text(_current_tools_json())
    print(f"Wrote baseline to {_BASELINE_PATH}")
