"""Pydantic models shared by ingest, tools, agents, API and UIs."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

Scalar = str | float | int | bool | None


# ---------------------------------------------------------------- taxonomy
@lru_cache
def taxonomy() -> dict:
    return yaml.safe_load((Path(__file__).parent / "taxonomy.yaml").read_text())["terms"]


def term_names() -> list[str]:
    return list(taxonomy())


def sub_term_paths() -> list[str]:
    return [f"{t}.{s}" for t, v in taxonomy().items() for s in v["sub_terms"]]


def known_leaves() -> dict[str, dict]:
    """leaf key -> {path: 'Term.Sub.key', unit, hint}"""
    out = {}
    for t, tv in taxonomy().items():
        for s, sv in tv["sub_terms"].items():
            for k, kv in (sv.get("leaves") or {}).items():
                out[k] = {"path": f"{t}.{s}.{k}", **kv}
    return out


PERIOD_LIMIT = r"(performance|fiscal|calendar)\s+year\s+\d{4}|\bfy\s?\d{2,4}\b|\b\d{4}\s+only\b|\buntil\s+\d{4}-\d{2}-\d{2}"


def is_period_limit(note: str | None) -> bool:
    """A scope note that makes a value temporary (e.g. 'Performance Year 2025 only'), as opposed to ordinary wording."""
    import re
    return bool(note and re.search(PERIOD_LIMIT, note, re.I))


# ---------------------------------------------------------------- digitization
class Section(BaseModel):
    section_id: str
    doc_id: str
    number: str | None = None
    heading: str
    path: list[str] = []  # heading path, e.g. ["Compensation", "Payout"]
    level: int = 1
    parent_id: str | None = None
    pages: list[int] = []
    text: str = ""
    tables_md: list[str] = []
    item_refs: list[str] = []
    bboxes: list[dict] = []  # [{page, l, t, r, b}]


class SectionEnrichment(BaseModel):
    section_id: str
    summary: str = Field(description="1-2 sentence plain summary of what the section says")
    term_tags: list[str] = Field(description="Taxonomy sub-term paths covered, e.g. 'Compensation.Payout'")
    defined_terms: list[str] = Field(default_factory=list, description="Capitalised terms this section defines")
    cross_refs: list[str] = Field(default_factory=list, description="Other clauses referenced, e.g. '4.3'")
    scope_notes: list[str] = Field(default_factory=list, description="Wording that limits scope or sets an effective date")


class DocEnrichment(BaseModel):
    doc_summary: str
    terms_covered: list[str]
    effective_date_in_text: str | None = Field(None, description="ISO date if the text states an effective date")
    doc_scope_note: str | None = Field(None, description="Document-wide scope limit, e.g. 'Performance Year 2025 only'")
    sections: list[SectionEnrichment]


# ---------------------------------------------------------------- evidence + extract
class Citation(BaseModel):
    doc_id: str
    section_id: str
    doc_date: str | None = None
    effective_date: str | None = None
    page: int | None = None
    quote: str
    docling_ref: str | None = None  # Docling item (e.g. "#/texts/16") the quote was found in
    bbox: dict | None = None  # {page, l, t, r, b, origin} from Docling provenance


class Finding(BaseModel):
    finding_id: int | None = None
    trader_id: str
    path: str  # Term.SubTerm.leaf_key
    value: Scalar
    unit: str | None = None
    assertion: Literal["set", "removed"] = "set"
    scope_note: str | None = None
    source: Citation
    quote_verified: bool | None = None
    run_id: str | None = None
    agent: str | None = None


class HistoryEntry(BaseModel):
    value: Scalar
    doc_id: str
    doc_date: str | None = None
    effective_date: str | None = None
    assertion: str = "set"


class Leaf(BaseModel):
    key: str
    value: Scalar
    unit: str | None = None
    status: Literal["current", "removed", "conflict", "needs_review"] = "current"
    source: Citation
    history: list[HistoryEntry] = []
    conflict: dict[str, Any] | None = None
    decision: str | None = None  # why this value won


class SubTermNode(BaseModel):
    sub_term: str
    leaves: list[Leaf] = []


class TermNode(BaseModel):
    term: str
    sub_terms: list[SubTermNode] = []


class Conflict(BaseModel):
    path: str
    kind: Literal["same_date_different_value", "scoped_override", "missing_date", "unverified_quote"]
    candidates: list[Finding]
    note: str = ""
    resolution: dict[str, Any] | None = None  # {action: pick|keep_prior|manual, finding_id, value, reason, by}


class TraderExtract(BaseModel):
    trader_id: str
    as_of: str
    version: int | None = None
    summary: str = ""
    terms: list[TermNode] = []
    conflicts: list[Conflict] = []
    documents: list[dict] = []
    run_id: str | None = None

    def leaf(self, path: str) -> Leaf | None:
        t, s, k = path.split(".", 2)
        for tn in self.terms:
            if tn.term == t:
                for sn in tn.sub_terms:
                    if sn.sub_term == s:
                        for lf in sn.leaves:
                            if lf.key == k:
                                return lf
        return None

    def flat(self) -> dict[str, Leaf]:
        return {f"{t.term}.{s.sub_term}.{lf.key}": lf for t in self.terms for s in t.sub_terms for lf in s.leaves}
