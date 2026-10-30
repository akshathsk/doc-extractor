"""Digitization Agent (enrichment): one structured LLM call per document batch, cached by prompt version.

Output is navigation metadata only (summaries, term tags, definitions, cross refs, scope notes). It is never
used as evidence: findings must quote raw section text, checked by verify_quote.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .. import llm
from ..config import settings
from ..db import j, q, q1, tx
from ..ingest import load_sections
from ..prompts import load
from ..schemas import DocEnrichment, SectionEnrichment, is_period_limit, sub_term_paths, taxonomy

BATCH = 40

# Offline keyword tagger (also a useful recall backstop in live mode).
KEYWORDS = {
    "Compensation.Base": ["base salary"],
    "Compensation.Payout": ["payout equal to", "payout shall be", "percent", "of net trading p&l"],
    "Compensation.Hurdle": ["hurdle"],
    "Compensation.HighWaterMark": ["high-water mark", "high water mark"],
    "Compensation.Netting": ["netted", "netting"],
    "Deferral.Schedule": ["deferred", "defer ", "deferral threshold"],
    "Deferral.Vesting": ["vest"],
    "Clawback.Terms": ["clawback", "recover payouts"],
    "Guarantee.Guarantee": ["guarantee", "no less than"],
    "Guarantee.Draw": ["draw"],
    "RestrictiveCovenants.NonCompete": ["competing business", "non-compet"],
    "RestrictiveCovenants.NonSolicit": ["solicit"],
    "Termination.Notice": ["notice", "garden leave"],
}


def _offline_tags(heading: str, text: str) -> list[str]:
    hay = f"{heading}\n{text}".lower()
    tags = [p for p, kws in KEYWORDS.items() if any(re.search(r"\b" + re.escape(k.strip()), hay) for k in kws)]
    if "Compensation.Payout" in tags and ("defer" in hay or "guarantee" in hay or "hurdle" in hay):
        if not re.search(r"payout (equal to|shall be)|% ?\) of net trading", hay):
            tags.remove("Compensation.Payout")
    if "solicit" in hay and "Compensation.Payout" in tags:
        tags.remove("Compensation.Payout")
    return tags


def _offline(doc: dict, sections: list) -> DocEnrichment:
    out = []
    eff = None
    scope = None
    for s in sections:
        text = s.text
        scope_notes = re.findall(r"[^.]*\b(?:only|effective \d{4}-\d{2}-\d{2}|deleted in (?:their|its) entirety)[^.]*\.", text)
        m = re.search(r"effective (\d{4}-\d{2}-\d{2})", text)
        if m and s.level == 0:
            eff = m.group(1)
        if s.level == 0:
            m2 = re.search(r"[^.]*apply for the Performance Year \d{4} only\.", text)
            scope = m2.group(0).strip() if m2 else None
        out.append(SectionEnrichment(
            section_id=s.section_id,
            summary=(text.split(". ")[0][:220] + ("." if text else "")) if text else f"Heading: {s.heading}",
            term_tags=[] if s.level == 0 else _offline_tags(s.heading, text),
            defined_terms=re.findall(r'"([A-Z][^"]{2,40})"(?: means|\))', text),
            cross_refs=re.findall(r"\b(?:clause|section)\s+(\d+(?:\.\d+)*)", text, re.I),
            scope_notes=[x.strip() for x in scope_notes]))
    covered = sorted({t.split(".")[0] for e in out for t in e.term_tags})
    return DocEnrichment(doc_summary=f"{doc['title']} dated {doc['doc_date']} for {doc['trader_id']} covering {', '.join(covered) or 'no taxonomy terms'}.",
                         terms_covered=covered, effective_date_in_text=eff, doc_scope_note=scope, sections=out)


def enrich_document(doc_id: str, force: bool = False) -> dict:
    tmpl, version = load("digitizer")
    model = settings.fast_model if settings.provider != "offline" else "offline"
    cached = q1("SELECT * FROM doc_enrichment WHERE doc_id=?", (doc_id,))
    if cached and cached["prompt_version"] == version and cached["model"] == model and not force:
        return {"cached": True}
    doc = q1("SELECT * FROM documents WHERE doc_id=?", (doc_id,))
    sections = load_sections(doc_id)
    allowed = set(sub_term_paths())
    system = tmpl.format(allowed="\n".join(f"- {p}" for p in sorted(allowed)))
    merged: DocEnrichment | None = None
    for i in range(0, len(sections), BATCH):
        batch = sections[i:i + BATCH]
        payload = {"document": {k: doc[k] for k in ("doc_id", "trader_id", "title", "doc_type", "doc_date")},
                   "sections": [{"section_id": s.section_id, "number": s.number, "heading": s.heading,
                                 "path": s.path, "text": s.text, "tables": s.tables_md} for s in batch]}
        res = llm.structured(name=f"digitizer:{doc_id}", system=system, user=json.dumps(payload),
                             schema=DocEnrichment, offline=lambda b=batch: _offline(doc, b), prompt_version=version)
        if merged is None:
            merged = res
        else:
            merged.sections += res.sections
            merged.terms_covered = sorted(set(merged.terms_covered) | set(res.terms_covered))
    assert merged is not None
    # guardrail: tags must exist in the taxonomy; unknown section ids are dropped
    ids = {s.section_id for s in sections}
    bad_tags = 0
    with tx() as c:
        for e in merged.sections:
            if e.section_id not in ids:
                continue
            tags = [t for t in e.term_tags if t in allowed]
            bad_tags += len(e.term_tags) - len(tags)
            c.execute("UPDATE sections SET summary=?, term_tags_json=?, defined_terms_json=?, cross_refs_json=?, scope_notes_json=?"
                      " WHERE section_id=?", (e.summary, j(tags), j(e.defined_terms), j(e.cross_refs), j(e.scope_notes), e.section_id))
        # terms_covered = LLM list ∪ section tags (the doc-level list alone can miss terms)
        tagged = {t.split(".")[0] for e in merged.sections for t in e.term_tags if t in allowed}
        terms = sorted({t for t in merged.terms_covered if t in taxonomy()} | tagged)
        # an effective-date sentence is not a scope limit
        # keep only period limits ("Performance Year 2025 only", "FY2025") - reconcile treats these as scoped overrides
        if not is_period_limit(merged.doc_scope_note):
            merged.doc_scope_note = None
        c.execute("INSERT INTO doc_enrichment(doc_id, doc_summary, terms_covered_json, effective_date_in_text, doc_scope_note, model,"
                  " prompt_version, enriched_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(doc_id) DO UPDATE SET"
                  " doc_summary=excluded.doc_summary, terms_covered_json=excluded.terms_covered_json,"
                  " effective_date_in_text=excluded.effective_date_in_text, doc_scope_note=excluded.doc_scope_note,"
                  " model=excluded.model, prompt_version=excluded.prompt_version, enriched_at=excluded.enriched_at",
                  (doc_id, merged.doc_summary, j(terms), merged.effective_date_in_text, merged.doc_scope_note, model, version,
                   datetime.now(timezone.utc).isoformat()))
    return {"cached": False, "sections": len(merged.sections), "dropped_unknown_tags": bad_tags,
            "effective_date_in_text": merged.effective_date_in_text, "doc_scope_note": merged.doc_scope_note}


def tags_for(section_ids: list[str]) -> dict[str, list[str]]:
    rows = q(f"SELECT section_id, term_tags_json FROM sections WHERE section_id IN ({','.join('?' * len(section_ids))})",
             tuple(section_ids)) if section_ids else []
    return {r["section_id"]: json.loads(r["term_tags_json"] or "[]") for r in rows}
