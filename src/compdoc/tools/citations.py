"""Build structured citations for a chat answer from the sources the agent actually retrieved.

Candidates come from tool results seen during the turn (extract leaves, read sections, search/finder hits,
clause variants, draft precedents). We keep the ones the answer refers to (by section id, doc id + clause number,
or doc id), then resolve each against the stored sections and the Docling JSON (item ref, page, bbox).
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..db import q1
from .access import can_see
from .verify import locate_quote


def _cand(doc_id: str | None, section_id: str | None, quote: str | None = None, page: int | None = None,
          docling_ref: str | None = None, bbox: dict | None = None, via: str = "") -> dict | None:
    if not doc_id or not section_id:
        return None
    return {"doc_id": doc_id, "section_id": section_id, "quote": quote, "page": page, "docling_ref": docling_ref, "bbox": bbox, "via": via}


def _from_leaf(lf: dict, via: str) -> dict | None:
    s = lf.get("source") or {}
    return _cand(s.get("doc_id"), s.get("section_id"), s.get("quote"), s.get("page"), s.get("docling_ref"), s.get("bbox"), via)


def candidates_from_calls(calls: list[dict]) -> list[dict]:
    out: list[dict | None] = []
    for c in calls:
        r, tool, args = c.get("result"), c.get("tool"), c.get("args") or {}
        if c.get("error") or r is None:
            continue
        if tool == "get_trader_extract" and isinstance(r, dict):
            for t in r.get("terms", []):
                for s in t.get("sub_terms", []):
                    for lf in s.get("leaves", []):
                        out.append(_from_leaf(lf, tool))
        elif tool == "get_term" and isinstance(r, dict):
            out += [_from_leaf(lf, tool) for lf in (r.get("leaves") or {}).values()]
        elif tool == "read_section" and isinstance(r, dict):
            out.append(_cand((r.get("doc") or {}).get("doc_id"), r.get("section_id"), None, (r.get("pages") or [None])[0], via=tool))
            for ch in r.get("children", []):
                out.append(_cand((r.get("doc") or {}).get("doc_id"), ch.get("section_id"), via=tool))
        elif tool == "read_chosen_sections" and isinstance(r, list):
            out += [_cand((x.get("doc") or {}).get("doc_id"), x.get("section_id"), None, (x.get("pages") or [None])[0], via=tool)
                    for x in r if not x.get("error")]
        elif tool in ("search_sections", "find_sections_by_term") and isinstance(r, list):
            out += [_cand(h.get("doc_id"), h.get("section_id"), via=tool) for h in r]
        elif tool == "find_docs_with_term" and isinstance(r, dict):
            out += [_cand(h.get("doc_id"), h.get("section_id"), via=tool) for h in r.get("results", [])]
        elif tool == "get_clause_variants" and isinstance(r, dict):
            for v in r.get("variants", []):
                out.append(_cand(v.get("latest_doc_id"), v.get("latest_section_id"), via=tool))
                out += [_cand(d.get("doc_id"), d.get("section_id"), via=tool) for d in v.get("docs", [])]
        elif tool == "render_docx":
            out += [_cand(cl.get("precedent_doc_id"), cl.get("precedent_section_id"), via="draft precedent")
                    for cl in args.get("clauses", [])]
    return [x for x in out if x]


def _mentioned(answer: str, c: dict) -> int:
    """2 = section id or doc+clause number in the answer, 1 = only the doc id, 0 = not mentioned."""
    sid, did = c["section_id"], c["doc_id"]
    num = sid.split(":s", 1)[1] if ":s" in sid else None
    if sid in answer:
        return 2
    if did in answer:
        if num and re.search(rf"{re.escape(did)}[^\]\n]{{0,40}}(?:§|s|section\s*)?{re.escape(num)}\b", answer):
            return 2
        return 1
    return 0


def build_citations(answer: str, calls: list[dict], caller: str = "system", limit: int = 12) -> list[dict]:
    cands = candidates_from_calls(calls)
    merged: dict[str, dict] = {}
    for c in cands:  # keep the richest candidate per section (leaf sources carry quote + docling ref)
        prev = merged.get(c["section_id"])
        if not prev or (c.get("quote") and not prev.get("quote")):
            merged[c["section_id"]] = c
    scored = [(_mentioned(answer, c), c) for c in merged.values()]
    strong = [c for s, c in scored if s == 2]
    weak_docs = {c["doc_id"] for s, c in scored if s == 1} - {c["doc_id"] for c in strong}
    picked = strong + [next(c for s, c in scored if c["doc_id"] == d and s == 1) for d in sorted(weak_docs)]
    if not picked and not re.search(r"\b(not entitled|could not find|cannot find)\b", answer, re.I):
        picked = list(merged.values())[:5]  # answer cited nothing explicitly: show what it was based on
    out = []
    for c in picked[:limit]:
        sec = q1("SELECT s.number, s.heading, s.text, s.pages_json, d.trader_id, d.title, d.doc_type, d.doc_date, d.format"
                 " FROM sections s JOIN documents d USING(doc_id) WHERE s.section_id=?", (c["section_id"],))
        if not sec:
            continue
        if not can_see(caller, sec["trader_id"]):
            continue
        quote = c.get("quote") or (sec["text"].split("\n")[0][:300] if sec["text"] else None)
        loc = {} if c.get("docling_ref") else (locate_quote(c["doc_id"], c["section_id"], quote) if quote else {})
        pages = json.loads(sec["pages_json"] or "[]")
        out.append({
            "n": len(out) + 1, "doc_id": c["doc_id"], "doc_title": sec["title"], "doc_type": sec["doc_type"],
            "doc_date": sec["doc_date"], "trader_id": sec["trader_id"], "section_id": c["section_id"],
            "section": f"{sec['number'] or ''} {sec['heading']}".strip(),
            "page": c.get("page") or loc.get("page") or (pages[0] if pages else None),
            "quote": quote, "docling_ref": c.get("docling_ref") or loc.get("docling_ref"),
            "bbox": c.get("bbox") or loc.get("bbox"), "format": sec["format"], "via": c["via"],
        })
    return out


def format_citations(cites: list[dict]) -> str:
    lines = []
    for c in cites:
        loc = f"p.{c['page']}" if c["page"] else "DOCX"
        lines.append(f"[{c['n']}] {c['doc_id']} · {c['doc_title']} · {c['doc_date']} · §{c['section']} · {loc}"
                     f"{' · docling ' + c['docling_ref'] if c['docling_ref'] else ''}\n    “{(c['quote'] or '')[:220]}”")
    return "\n".join(lines)


def to_json(cites: list[dict[str, Any]]) -> str:
    return json.dumps(cites, default=str)
