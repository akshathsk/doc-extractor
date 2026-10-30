"""Doc Readers (scaling plan, Part 3): small agents, one per (document, term group), bounded context.

  route()          code: which documents touch which terms (enrichment, tags, keyword + semantic agreement);
                   short documents -> one work item for all relevant terms; long ones -> one per term group
  run_doc_reader() LLM agent bound to ONE document: shortlist + two-level outline -> choose -> read -> record
  coverage_gaps()  code: strong evidence of a term that no reader read and no finding covers -> re-queue once
A researcher never needs two documents at once: the Reconciler compares documents by date afterwards.
"""
from __future__ import annotations

import json

from .. import llm
from ..db import q
from ..llm import ToolBox
from ..observability import current_run_id
from ..prompts import load
from ..schemas import taxonomy
from ..tools.docs import get_article_outline
from ..tools.hybrid import hybrid_search
from ..tools.registry import doc_reader_tools
from .offline_rules import extract_values

import os

# A (document, term) pair is relevant when the enrichment lists the term, a section is tagged with it, or a section's
# semantic similarity to the term reaches SEM_MIN. Ranks alone mean little in short documents (something always ranks
# first). 0.55 kept every true pair with ~12% false pairs on the synthetic sets (semantic score study); re-tune on real data.
SEM_MIN = float(os.environ.get("COMPDOC_ROUTE_SEM_MIN", "0.55"))
TERM_GROUPS = [["Compensation", "Guarantee"], ["Deferral", "Clawback"], ["RestrictiveCovenants", "Termination"]]
LARGE_DOC_SECTIONS = 80
PER_SUBTERM = 4


def _terms() -> list[str]:
    return [t for t in taxonomy() if t != "Other"]


def route(trader_id: str, inventory: list[dict]) -> list[dict]:
    """Work items: {doc_id, terms, shortlist, evidence}. Only relevant (document, term) pairs get an agent."""
    items = []
    for d in inventory:
        doc_id = d["doc_id"]
        n_sections = q("SELECT count(*) n FROM sections WHERE doc_id=? AND level > 0", (doc_id,))[0]["n"]
        covered = set(d.get("terms_covered") or [])
        cands: dict[str, dict] = {}
        evidence: dict[str, str] = {}
        for term in _terms():
            strong = term in covered
            weak = False
            for sub in taxonomy()[term]["sub_terms"]:
                for h in hybrid_search(term_path=f"{term}.{sub}", doc_ids=[doc_id], top_k=PER_SUBTERM):
                    similar = (h.get("semantic_score") or 0) >= SEM_MIN
                    if "tag" in h["found_by"] or similar:
                        strong = True
                        c = cands.setdefault(h["section_id"], {k: h.get(k) for k in ("section_id", "number", "heading")} |
                                             {"summary": (h.get("summary") or "")[:160], "found_by": set(), "terms": set()})
                        c["found_by"].update(h["found_by"])
                        c["terms"].add(term)
                    else:
                        weak = True
            if strong:
                evidence[term] = "strong"
            elif weak:
                evidence[term] = "weak"
        relevant = [t for t, e in evidence.items() if e == "strong"]
        if not relevant:
            continue
        groups = [relevant] if n_sections <= LARGE_DOC_SECTIONS else \
            [[t for t in g if t in relevant] for g in TERM_GROUPS if any(t in relevant for t in g)]
        for terms in groups:
            sl = [{**c, "found_by": sorted(c["found_by"]), "terms": sorted(c["terms"])} for c in cands.values() if c["terms"] & set(terms)]
            items.append({"doc_id": doc_id, "doc": {k: d.get(k) for k in ("doc_id", "title", "doc_type", "doc_date",
                                                                           "effective_date_in_text", "doc_scope_note")},
                          "terms": terms, "shortlist": sl, "evidence": {t: evidence[t] for t in terms}, "n_sections": n_sections})
    return items


def run_doc_reader(trader_id: str, item: dict, round_no: int = 1) -> dict:
    tmpl, version = load("doc_reader")
    doc, terms, doc_id = item["doc"], item["terms"], item["doc_id"]
    leaves = "\n".join(f"- {t}.{s}.{k} ({v.get('unit')}): {v.get('hint')}" for t in terms
                       for s, sv in taxonomy()[t]["sub_terms"].items() for k, v in (sv.get("leaves") or {}).items())
    outline = get_article_outline(doc_id)
    outline_txt = "\n".join(f"- {a['section_id']} | {a['number'] or ''} {a['heading']} | {a['sections_inside']} sections | "
                            f"tags {', '.join(a['term_tags'])} | {a['summary']}" for a in outline)
    shortlist_txt = "\n".join(f"- {c['section_id']} | {c['number'] or ''} {c['heading']} | terms {', '.join(c['terms'])} | "
                              f"found_by {', '.join(c['found_by'])} | {c['summary']}" for c in item["shortlist"]) or "(none)"
    eff = f" · effective {doc['effective_date_in_text']}" if doc.get("effective_date_in_text") else ""
    scope = f" · scope: {doc['doc_scope_note']}" if doc.get("doc_scope_note") else ""
    system = tmpl.format(doc_id=doc_id, title=doc["title"], doc_type=doc["doc_type"], doc_date=doc["doc_date"], effective=eff,
                         scope=scope, trader_id=trader_id, leaves=leaves, outline=outline_txt, shortlist=shortlist_txt)
    agent = f"doc_reader:{doc_id}:{'+'.join(terms)}" + (f":r{round_no}" if round_no > 1 else "")

    def offline(box: ToolBox) -> str:
        choices = [{"section_id": c["section_id"], "reason": f"shortlist: {', '.join(c['found_by'])}"} for c in item["shortlist"]]
        secs = box.call("read_chosen_sections", choices=choices) if choices else []
        n = 0
        for sec in secs:
            if "error" in sec:
                continue
            e = sec.get("effective_date")
            e = e if e and e != sec["doc"]["doc_date"] else None
            for term in terms:
                for f in extract_values(sec["text"], sec["heading"], term):
                    r = box.call("record_finding", path=f["path"], value=f["value"], quote=f["quote"], section_id=sec["section_id"],
                                 assertion=f["assertion"], effective_date=e, scope_note=doc.get("doc_scope_note"))
                    n += int(bool(r.get("recorded")))
        return f"Offline reader for {doc_id} ({', '.join(terms)}): read {len(choices)} shortlisted sections, recorded {n} findings."

    res = llm.run_agent(name=agent, system=system, tools=doc_reader_tools(trader_id, doc_id, agent), offline_policy=offline,
                        prompt_version=version, user=f"Read {doc_id} for {', '.join(terms)} and record every value it states.")
    read = []
    for c in res.tool_calls:
        if c["tool"] == "read_chosen_sections":
            read += [x["section_id"] for x in c["args"].get("choices", [])]
        elif c["tool"] == "read_section":
            read.append(c["args"].get("section_id"))
    recorded = sum(1 for c in res.tool_calls if c["tool"] == "record_finding" and not c["error"]
                   and (c.get("result") or {}).get("recorded"))
    return {"doc_id": doc_id, "terms": terms, "round": round_no, "read": sorted(set(filter(None, read))), "recorded": recorded,
            "summary": res.final_text, "steps": res.steps, "tool_calls": len(res.tool_calls),
            "shortlist": [c["section_id"] for c in item["shortlist"]]}


def coverage_gaps(trader_id: str, items: list[dict], reports: list[dict]) -> list[dict]:
    """Strong evidence of a term in a document, but its strong sections were never read and no finding covers it."""
    run_id = current_run_id()
    found = {(json.loads(r["source_json"])["doc_id"], r["path"].split(".")[0])
             for r in q("SELECT path, source_json FROM findings WHERE run_id=? AND trader_id=?", (run_id, trader_id))}
    read_by_doc: dict[str, set] = {}
    for rep in reports:
        read_by_doc.setdefault(rep["doc_id"], set()).update(rep["read"])
    gaps = []
    for it in items:
        for term in it["terms"]:
            if (it["doc_id"], term) in found:
                continue
            unread = [c for c in it["shortlist"] if term in c["terms"] and c["section_id"] not in read_by_doc.get(it["doc_id"], set())
                      and "tag" in c["found_by"]]
            if unread:
                gaps.append({**it, "terms": [term], "shortlist": unread,
                             "gap_reason": f"{len(unread)} tagged {term} section(s) never read and no {term} finding"})
    return gaps
