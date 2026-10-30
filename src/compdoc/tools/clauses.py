"""Cross-trader clause tools (Term Finder + Drafting): which docs contain a term, and its wording variants."""
from __future__ import annotations

import json
import re

from ..db import q
from .access import allowed_traders

AMEND_PREFIX = re.compile(r"^the [\w\s-]+ provisions of the agreement are deleted and replaced with the following:\s*", re.I)


def _rows(term_path: str, trader_id: str | None, doc_type: str | None, date_from: str | None, date_to: str | None) -> list[dict]:
    sql = ("SELECT s.section_id, s.doc_id, s.number, s.heading, s.summary, s.text, s.term_tags_json, s.level,"
           " d.trader_id, d.trader_name, d.doc_date, d.doc_type, d.title FROM sections s JOIN documents d USING(doc_id) WHERE 1=1")
    params: list = []
    if trader_id:
        sql += " AND d.trader_id=?"
        params.append(trader_id)
    if doc_type:
        sql += " AND d.doc_type=?"
        params.append(doc_type)
    if date_from:
        sql += " AND d.doc_date>=?"
        params.append(date_from)
    if date_to:
        sql += " AND d.doc_date<=?"
        params.append(date_to)
    out = []
    for r in q(sql + " ORDER BY d.doc_date DESC, s.ord", tuple(params)):
        tags = json.loads(r["term_tags_json"] or "[]")
        if r["level"] > 0 and r["text"].strip() and any(t == term_path or t.startswith(term_path + ".") for t in tags):
            out.append(r)
    return out


def find_docs_with_term(term_path: str, trader_id: str | None = None, doc_type: str | None = None,
                        date_from: str | None = None, date_to: str | None = None, caller: str = "system") -> dict:
    """Documents containing a term (by enrichment tags), newest first. Works across traders if trader_id is omitted.
    Clause text/summary is withheld for traders the caller is not entitled to."""
    allowed = allowed_traders(caller)
    results, withheld = [], 0
    for r in _rows(term_path, trader_id, doc_type, date_from, date_to):
        ok = allowed is None or r["trader_id"] in allowed
        withheld += 0 if ok else 1
        results.append({"doc_id": r["doc_id"], "trader_id": r["trader_id"], "doc_date": r["doc_date"], "doc_type": r["doc_type"],
                        "doc_title": r["title"], "section_id": r["section_id"], "number": r["number"], "heading": r["heading"],
                        "summary": r["summary"] if ok else None, "snippet": r["text"][:240] if ok else None,
                        "entitled": ok})
    return {"term_path": term_path, "count": len(results), "documents": len({x["doc_id"] for x in results}),
            "withheld_text_for": withheld, "results": results}


def _canon(text: str) -> str:
    t = AMEND_PREFIX.sub("", text.strip().split("\n")[0])
    t = re.sub(r"us\$[\d,]+(\.\d+)?", "<amount>", t, flags=re.I)
    t = re.sub(r"\b[a-z-]+ (percent )?\(\d+%?\)", "<n>", t, flags=re.I)
    t = re.sub(r"\d[\d,.]*", "<n>", t)
    return re.sub(r"\s+", " ", t.lower()).strip()


def _shingles(t: str, k: int = 3) -> set:
    w = t.split()
    return {" ".join(w[i:i + k]) for i in range(max(1, len(w) - k + 1))}


def get_clause_variants(term_path: str, trader_id: str | None = None, threshold: float = 0.6, caller: str = "system") -> dict:
    """Group sections tagged with term_path into wording variants (3-word shingle Jaccard), newest first."""
    allowed = allowed_traders(caller)
    variants: list[dict] = []
    for r in _rows(term_path, trader_id, None, None, None):
        c = _canon(r["text"])
        sh = _shingles(c)
        best = None
        for v in variants:
            jac = len(sh & v["_sh"]) / max(1, len(sh | v["_sh"]))
            if jac >= threshold and (best is None or jac > best[0]):
                best = (jac, v)
        ok = allowed is None or r["trader_id"] in allowed
        member = {"doc_id": r["doc_id"], "trader_id": r["trader_id"], "doc_date": r["doc_date"], "section_id": r["section_id"],
                  "doc_type": r["doc_type"], "text": r["text"] if ok else None}
        if best:
            best[1]["docs"].append(member)
        else:
            variants.append({"_sh": sh, "template": c, "canonical_text": r["text"] if ok else None,
                             "latest_doc_date": r["doc_date"], "latest_section_id": r["section_id"],
                             "latest_doc_id": r["doc_id"], "docs": [member]})
    out = []
    for i, v in enumerate(sorted(variants, key=lambda v: (-len(v["docs"]), v["latest_doc_date"]), reverse=False), 1):
        v.pop("_sh")
        out.append({"variant_id": f"V{i}", "doc_count": len({d['doc_id'] for d in v["docs"]}), **v})
    out.sort(key=lambda v: v["latest_doc_date"], reverse=True)
    return {"term_path": term_path, "variants": out, "latest_variant": out[0]["variant_id"] if out else None}
