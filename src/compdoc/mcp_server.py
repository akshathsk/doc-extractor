"""MCP server exposing the same tools to Claude Desktop / Cursor / any MCP client (stdio).

Run:  uv run python -m compdoc.mcp_server
Caller identity for entitlements comes from COMPDOC_MCP_CALLER (default: analyst_a).
"""
from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from .tools import clauses, docs, extracts, search

CALLER = os.environ.get("COMPDOC_MCP_CALLER", "analyst_a")
server = MCPServer("compdoc", instructions="Trader compensation contract tools: published term trees, documents, clause search.")


@server.tool()
def get_trader_extract(trader_id: str) -> dict[str, Any]:
    """Latest published term tree for a trader: current values with sources, history and open conflicts."""
    return extracts.get_trader_extract(trader_id, CALLER)


@server.tool()
def get_term(trader_id: str, path: str) -> dict[str, Any]:
    """One term (e.g. 'Deferral'), sub-term ('Deferral.Vesting') or leaf from the latest extract."""
    return extracts.get_term(trader_id, path, CALLER)


@server.tool()
def list_trader_docs(trader_id: str) -> list[dict[str, Any]]:
    """A trader's documents, oldest first, with dates, summaries and terms covered."""
    return docs.list_trader_docs(trader_id, CALLER)


@server.tool()
def read_section(doc_id: str, section_id: str) -> dict[str, Any]:
    """Full text of one section with page numbers, definitions and cross-referenced clauses."""
    return docs.read_section(doc_id, section_id, CALLER)


@server.tool()
def search_sections(query: str, trader_id: str | None = None, top_k: int = 8) -> list[dict[str, Any]]:
    """Keyword (BM25) search over contract sections."""
    return search.search_sections(query, trader_id, None, top_k, CALLER)


@server.tool()
def find_docs_with_term(term_path: str, trader_id: str | None = None) -> dict[str, Any]:
    """Documents containing a term (e.g. 'Clawback.Terms'), newest first, across traders."""
    return clauses.find_docs_with_term(term_path, trader_id, caller=CALLER)


@server.tool()
def get_clause_variants(term_path: str) -> dict[str, Any]:
    """Distinct wording variants of a clause type and which is the latest."""
    return clauses.get_clause_variants(term_path, caller=CALLER)


if __name__ == "__main__":
    server.run("stdio")
