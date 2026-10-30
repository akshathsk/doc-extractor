"""Tool registry: typed inputs (Pydantic) + per-agent tool sets with trader/caller bound in.

Binding trader_id and caller in closures means an agent cannot read another trader's documents or act as a
different caller, regardless of what the model passes.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..drafting.render_docx import render_docx
from ..llm import Tool
from ..schemas import Scalar
from . import clauses, docs, extracts, ledger, search, verify


# ------------------------------------------------------------------------------------------ input models
class TermPathIn(BaseModel):
    term_path: str = Field(description="Taxonomy path, e.g. 'Compensation' or 'Compensation.Payout'")


class SearchIn(BaseModel):
    query: str = Field(description="Keywords, e.g. 'payout percent net trading P&L'")
    doc_ids: list[str] | None = Field(None, description="Optional: restrict to these documents")
    top_k: int = Field(8, ge=1, le=25)


class SearchAnyIn(SearchIn):
    trader_id: str | None = Field(None, description="Optional trader to restrict to")


class DocIn(BaseModel):
    doc_id: str


class SectionIn(BaseModel):
    doc_id: str
    section_id: str


class OutlinesIn(BaseModel):
    doc_ids: list[str] = Field(description="Documents to outline (usually all of the trader's documents)")


class Choice(BaseModel):
    doc_id: str
    section_id: str
    reason: str = Field(description="Why this section is relevant to the term (one short sentence)")


class ChooseIn(BaseModel):
    choices: list[Choice] = Field(description="Sections chosen from the outlines, each with a reason")


class RecordIn(BaseModel):
    path: str = Field(description="Term.SubTerm.leaf_key, e.g. 'Compensation.Payout.payout_pct_net_pnl'")
    value: Scalar = Field(description="Normalised value: number for %, money, periods; true/false; short text; null if removed")
    unit: str | None = Field(None, description="pct | usd | months | years | days | bool | text")
    quote: str = Field(description="Exact verbatim quote from the section text containing the value")
    doc_id: str
    section_id: str
    assertion: Literal["set", "removed"] = "set"
    scope_note: str | None = Field(None, description="Scope limit, e.g. 'Performance Year 2025 only'")
    effective_date: str | None = Field(None, description="ISO date if the document states its own effective date")


class FindingsIn(BaseModel):
    path: str | None = None


class VerifyIn(BaseModel):
    doc_id: str
    section_id: str
    quote: str
    value: Scalar = None


class TraderIn(BaseModel):
    trader_id: str


class TermIn(BaseModel):
    trader_id: str
    path: str = Field(description="Term, Term.SubTerm or Term.SubTerm.leaf_key")


class CompareIn(BaseModel):
    trader_id: str
    from_version: int
    to_version: int


class FindDocsIn(BaseModel):
    term_path: str
    trader_id: str | None = None
    doc_type: str | None = Field(None, description="base_agreement | amendment | side_letter | memo")
    date_from: str | None = None
    date_to: str | None = None


class VariantsIn(BaseModel):
    term_path: str
    trader_id: str | None = None


class DraftClause(BaseModel):
    heading: str
    text: str
    term_path: str | None = None
    precedent_doc_id: str | None = None
    precedent_section_id: str | None = None
    new_language: bool = False
    old_value: Scalar = None
    new_value: Scalar = None


class RenderIn(BaseModel):
    doc_type: Literal["amendment", "side_letter"]
    title: str
    clauses: list[DraftClause]


class NoIn(BaseModel):
    pass


# ------------------------------------------------------------------------------------------ tool sets
def _read(caller: str) -> Tool:
    return Tool("read_section", "Read a section's full text (with children, tables, page numbers, relevant definitions and "
                "cross-referenced clauses). Base all findings and quotes on this text.", SectionIn,
                lambda doc_id, section_id: docs.read_section(doc_id, section_id, caller))


def _trader_docs(trader_id: str) -> set[str]:
    from ..db import q
    return {r["doc_id"] for r in q("SELECT doc_id FROM documents WHERE trader_id=?", (trader_id,))}


class SectionOnlyIn(BaseModel):
    section_id: str


class DocChoice(BaseModel):
    section_id: str
    reason: str = Field(description="Why this section is relevant (one short sentence)")


class DocChooseIn(BaseModel):
    choices: list[DocChoice] = Field(description="Sections of this document chosen to read, each with a reason")


class ArticleIn(BaseModel):
    article_id: str = Field(description="section_id of an article from the article outline, e.g. 'L1-001:sart8'")


class QueryIn(BaseModel):
    query: str = Field(description="Your own words for what you are looking for")
    top_k: int = Field(8, ge=1, le=20)


class DocRecordIn(BaseModel):
    path: str = Field(description="Term.SubTerm.leaf_key, e.g. 'Compensation.Payout.payout_pct_net_pnl'")
    value: Scalar = Field(description="Normalised value: number for %, money, periods; true/false; short text; null if removed")
    unit: str | None = Field(None, description="pct | usd | months | years | days | bool | text")
    quote: str = Field(description="Exact verbatim quote from the section text containing the value")
    section_id: str
    assertion: Literal["set", "removed"] = "set"
    scope_note: str | None = Field(None, description="Scope limit, e.g. 'Performance Year 2025 only'")
    effective_date: str | None = Field(None, description="ISO date if the document states its own effective date")


def doc_reader_tools(trader_id: str, doc_id: str, agent: str) -> list[Tool]:
    """Bound to ONE document: a Doc Reader cannot read or record anything outside it."""
    from . import hybrid

    def mine(sid: str) -> bool:
        return sid.startswith(doc_id + ":")
    return [
        Tool("get_article_sections", "Level 2 of the outline: the sections inside one article (number, heading, summary, tags; "
             "no body text).", ArticleIn, lambda article_id: docs.get_article_sections(doc_id, article_id)),
        Tool("read_chosen_sections", "Read the full text of the sections you chose, each with a reason. Base findings only on text "
             "returned here or by read_section.", DocChooseIn,
             lambda choices: docs.read_chosen_sections([{**c, "doc_id": doc_id} for c in choices if mine(c["section_id"])])),
        Tool("read_section", "Read one more section of this document (e.g. a cross-referenced clause or schedule).", SectionOnlyIn,
             lambda section_id: docs.read_section(doc_id, section_id) if mine(section_id) else {"error": "other document"}),
        Tool("search_in_doc", "Hybrid search (tags + keyword + semantic) inside this document, in your own words.", QueryIn,
             lambda query, top_k=8: hybrid.hybrid_search(query=query, doc_ids=[doc_id], top_k=top_k)),
        Tool("record_finding", "Record one value found in a section of this document (once per value per section). "
             "The quote is verified against the section text.", DocRecordIn,
             lambda **kw: ledger.record_finding(trader_id=trader_id, agent=agent, doc_id=doc_id, **kw)
             if mine(kw["section_id"]) else {"recorded": False, "error": f"{kw['section_id']} is not in {doc_id}"}),
    ]


def reconciler_tools(trader_id: str) -> list[Tool]:
    return [
        _read("system"),
        Tool("verify_quote", "Check that a quote (and value) appears in the cited section.", VerifyIn,
             lambda doc_id, section_id, quote, value=None: verify.verify_quote(doc_id, section_id, quote, value)),
        Tool("get_findings", f"Ledger findings for {trader_id} in this run, optionally for one path.", FindingsIn,
             lambda path=None: [f.model_dump() for f in ledger.get_findings(trader_id, path=path)]),
    ]


def qa_tools(caller: str) -> list[Tool]:
    return [
        Tool("get_trader_extract", "Latest published term tree for a trader (current values, sources, history, conflicts).",
             TraderIn, lambda trader_id: extracts.get_trader_extract(trader_id, caller)),
        Tool("get_term", "One term, sub-term or leaf from the latest published extract.", TermIn,
             lambda trader_id, path: extracts.get_term(trader_id, path, caller)),
        Tool("list_trader_docs", "A trader's documents (oldest first) with dates, summaries and terms covered.", TraderIn,
             lambda trader_id: docs.list_trader_docs(trader_id, caller)),
        Tool("search_sections", "Keyword search over sections; pass trader_id to restrict to one trader.", SearchAnyIn,
             lambda query, trader_id=None, doc_ids=None, top_k=8: search.search_sections(query, trader_id, doc_ids, top_k, caller)),
        _read(caller),
        Tool("compare_versions", "Diff two published extract versions of a trader.", CompareIn,
             lambda trader_id, from_version, to_version: extracts.compare_versions(trader_id, from_version, to_version, caller)),
    ]


def finder_tools(caller: str) -> list[Tool]:
    return [
        Tool("find_docs_with_term", "Documents (any trader unless trader_id given) containing a term, newest first.", FindDocsIn,
             lambda **kw: clauses.find_docs_with_term(caller=caller, **kw)),
        Tool("get_clause_variants", "Distinct wording variants of a term's clauses with doc counts and the latest variant.",
             VariantsIn, lambda term_path, trader_id=None: clauses.get_clause_variants(term_path, trader_id, caller=caller)),
        _read(caller),
        Tool("search_sections", "Keyword search over sections across traders.", SearchAnyIn,
             lambda query, trader_id=None, doc_ids=None, top_k=8: search.search_sections(query, trader_id, doc_ids, top_k, caller)),
    ]


def drafter_tools(trader_id: str, caller: str, request: str) -> list[Tool]:
    return [
        Tool("get_trader_extract", f"Current published terms of {trader_id}.", NoIn,
             lambda: extracts.get_trader_extract(trader_id, caller)),
        Tool("get_clause_variants", "Precedent wording variants for a term across all documents.", VariantsIn,
             lambda term_path, trader_id=None: clauses.get_clause_variants(term_path, trader_id, caller=caller)),
        Tool("find_docs_with_term", "Documents containing a term, newest first.", FindDocsIn,
             lambda **kw: clauses.find_docs_with_term(caller=caller, **kw)),
        _read(caller),
        Tool("render_docx", "Render the draft .docx (call once, with all clauses).", RenderIn,
             lambda doc_type, title, clauses: render_docx(trader_id, doc_type, title, clauses, request, caller)),
    ]


def playground_tools(caller: str = "system") -> dict[str, Tool]:
    """Every tool, for the Dev Console playground and the MCP server."""
    tools = qa_tools(caller) + finder_tools(caller) + [
        Tool("find_sections_by_term", "Sections of a trader tagged with a term.", TermIn,
             lambda trader_id, path: docs.find_sections_by_term(trader_id, path, caller)),
        Tool("get_section_outline", "Heading tree of one document.", DocIn, lambda doc_id: docs.get_section_outline(doc_id, caller)),
        Tool("verify_quote", "Check a quote against a section.", VerifyIn,
             lambda doc_id, section_id, quote, value=None: verify.verify_quote(doc_id, section_id, quote, value)),
    ]
    return {t.name: t for t in tools}
