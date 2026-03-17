"""sage-memory MCP server.

5 tools designed for LLM-authored content:
  memory_store   — persist understanding, decisions, patterns
  memory_search  — find relevant knowledge across project + global
  memory_update  — refine existing knowledge
  memory_delete  — remove outdated knowledge
  memory_list    — browse what's stored

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
from .db import get_project_name, close_all

logger = logging.getLogger("sage-memory")

TOOLS = [
    types.Tool(
        name="memory_store",
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
        name="memory_search",
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
                    "description": "Optional tags to boost matching results.",
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
        name="memory_update",
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
        name="memory_delete",
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
        name="memory_list",
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
]

# Dict-based dispatch
HANDLERS = {
    "memory_store": store,
    "memory_search": search,
    "memory_update": update,
    "memory_delete": delete,
    "memory_list": list_memories,
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
            if name in ("memory_store", "memory_search", "memory_list"):
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
