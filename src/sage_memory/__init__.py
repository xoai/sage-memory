"""sage-memory — Ultrafast local MCP memory for LLMs."""

import asyncio
from .server import run


def main():
    asyncio.run(run())
