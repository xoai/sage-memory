"""sage-memory MCP server.

7 tools, namespaced to avoid collision with client built-in memory:
  sage_memory_store   — persist understanding, decisions, patterns
  sage_memory_search  — find relevant knowledge across project + global
  sage_memory_update  — refine existing knowledge
  sage_memory_delete  — remove outdated knowledge
  sage_memory_list    — browse what's stored
  sage_memory_link    — create typed edges between memories
  sage_memory_graph   — traverse relationships across memories

Tool descriptions guide the LLM to produce high-quality, retrievable content.
The server auto-detects the project from the working directory.
"""

from __future__ import annotations

import json
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

from .store import store, update, delete, list_memories
from .search import search, flush_all_access
from .graph import link, graph
from .db import get_project_name, close_all

logger = logging.getLogger("sage-memory")

TOOLS = [
    types.Tool(
        name="sage_memory_store",
        description=(
            "Store knowledge for future retrieval. Use this to persist: "
            "code understanding (architecture, patterns, data flows), "
            "decisions and their rationale, "
            "debugging insights and solutions, "
            "project conventions and rules. "
            "Write a clear, descriptive title (what is this about?) and "
            "detailed content explaining the 'what' and 'why'. "
            "Good content is specific, uses domain vocabulary, and would "
            "help someone (or you, later) understand the topic without "
            "reading the source code."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "Detailed explanation of the knowledge. Include: "
                        "what it does, why it matters, key patterns or gotchas. "
                        "Use the project's actual terminology — class names, "
                        "function names, domain concepts. Markdown supported."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Short descriptive title (5-15 words). Be specific: "
                        "'Payment saga orchestration in billing service' not "
                        "'How payments work'."
                    ),
                },
                "tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Domain tags for filtering: technology, area, concept.",
                },
                "scope": {
                    "type": "string", "enum": ["project", "global"],
                    "description": (
                        "'project' (default) for this codebase's knowledge, "
                        "'global' for cross-project patterns and preferences."
                    ),
                },
            },
            "required": ["content"],
        },
    ),
    types.Tool(
        name="sage_memory_search",
        description=(
            "Search stored knowledge using natural language. "
            "Searches this project's memory and global memory, "
            "with project results ranked higher. "
            "Use before starting work to recall relevant context, "
            "architecture decisions, or past solutions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What you're looking for — describe the topic or question naturally.",
                },
                "tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Soft boost — results with these tags rank higher, but non-matching results are still included.",
                },
                "filter_tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": (
                        "Hard filter (AND logic) — ONLY return memories matching ALL these tags. "
                        "Use for namespace isolation, e.g. filter_tags: [\"self-learning\"] "
                        "to search only within learnings."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (1-50, default 5).",
                },
                "scope": {
                    "type": "string", "enum": ["project", "global"],
                    "description": "'project' (default) searches project + global. 'global' searches global only.",
                },
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="sage_memory_update",
        description=(
            "Update existing knowledge by ID. Use when understanding deepens, "
            "code changes, or stored information becomes outdated. "
            "Only provide fields you want to change."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory ID to update."},
                "content": {"type": "string", "description": "New content."},
                "title": {"type": "string", "description": "New title."},
                "tags": {"type": "array", "items": {"type": "string"}},
                "scope": {
                    "type": "string", "enum": ["project", "global"],
                    "description": "Which database contains this memory.",
                },
            },
            "required": ["id"],
        },
    ),
    types.Tool(
        name="sage_memory_delete",
        description="Delete a memory by ID. Use when knowledge is no longer relevant.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory ID to delete."},
                "scope": {
                    "type": "string", "enum": ["project", "global"],
                    "description": "Which database contains this memory.",
                },
            },
            "required": ["id"],
        },
    ),
    types.Tool(
        name="sage_memory_list",
        description=(
            "Browse stored memories with optional tag filtering. "
            "Shows what knowledge exists, sorted by most recently updated. "
            "Tags use AND logic: all specified tags must match."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string", "enum": ["project", "global"],
                    "description": "Which database to browse (default: project).",
                },
                "tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Filter by tags (AND logic — all must match).",
                },
                "limit": {"type": "integer", "description": "Page size (default 20)."},
                "offset": {"type": "integer", "description": "Pagination offset."},
            },
        },
    ),
    types.Tool(
        name="sage_memory_link",
        description=(
            "Create or delete a typed relationship (edge) between two memories. "
            "Use this to express: dependencies (A depends_on B), containment "
            "(project has_task task), ownership (task assigned_to person), "
            "blocking (task blocks task), or any directed relationship. "
            "Edges are automatically cleaned up when either memory is deleted."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "source_id": {"type": "string", "description": "ID of the source memory (edge starts here)."},
                "target_id": {"type": "string", "description": "ID of the target memory (edge points here)."},
                "relation": {
                    "type": "string",
                    "description": "Relationship type: depends_on, has_task, assigned_to, blocks, part_of, contains, relates_to, or custom.",
                },
                "properties": {
                    "type": "object",
                    "description": "Optional JSON properties on the edge (confidence, notes, etc.).",
                },
                "delete": {
                    "type": "boolean",
                    "description": "If true, delete the edge instead of creating it.",
                },
                "scope": {
                    "type": "string", "enum": ["project", "global"],
                    "description": "Which database (default: project).",
                },
            },
            "required": ["source_id", "target_id", "relation"],
        },
    ),
    types.Tool(
        name="sage_memory_graph",
        description=(
            "Traverse relationships from a starting memory. Returns connected "
            "memories and edges within the specified depth. Use to explore: "
            "dependency chains, project task trees, blocking relationships, "
            "or any graph structure built with sage_memory_link."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Starting memory ID."},
                "relation": {
                    "type": "string",
                    "description": "Optional — only follow edges of this relation type.",
                },
                "direction": {
                    "type": "string", "enum": ["outbound", "inbound", "both"],
                    "description": "outbound (source→target), inbound (target→source), or both. Default: outbound.",
                },
                "depth": {
                    "type": "integer",
                    "description": "Max traversal hops (1-5, default 1).",
                },
                "scope": {
                    "type": "string", "enum": ["project", "global"],
                    "description": "Which database (default: project).",
                },
            },
            "required": ["id"],
        },
    ),
]

# Dict-based dispatch
HANDLERS = {
    "sage_memory_store": store,
    "sage_memory_search": search,
    "sage_memory_update": update,
    "sage_memory_delete": delete,
    "sage_memory_list": list_memories,
    "sage_memory_link": link,
    "sage_memory_graph": graph,
}


def create_server() -> Server:
    server = Server("sage-memory")

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return TOOLS

    @server.call_tool()
    async def _call(name: str, arguments: dict) -> list[types.TextContent]:
        handler = HANDLERS.get(name)
        if not handler:
            return [types.TextContent(type="text",
                    text=json.dumps({"error": f"Unknown tool: {name}"}))]
        try:
            result = handler(**(arguments or {}))

            # Enrich response with project context
            if name in ("sage_memory_store", "sage_memory_search", "sage_memory_list"):
                project = get_project_name()
                if project:
                    result["_project"] = project

            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
        except Exception as e:
            logger.exception("Tool error: %s", name)
            return [types.TextContent(type="text",
                    text=json.dumps({"error": str(e)}))]

    return server


async def run() -> None:
    server = create_server()
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        flush_all_access()
        close_all()
