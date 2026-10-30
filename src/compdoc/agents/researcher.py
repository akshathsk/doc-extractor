"""Term Researcher: one per top-level term. Searches ALL of the trader's documents, reads sections, records findings."""
from __future__ import annotations

import json

from .. import llm
from ..llm import ToolBox
from ..prompts import load
from ..schemas import taxonomy
from ..tools.registry import researcher_tools
from .offline_rules import PATTERNS, extract_values


def run_researcher(trader_id: str, term: str, inventory: list[dict]) -> dict:
    tmpl, version = load("researcher")
    tdef = taxonomy()[term]
    leaves = "\n".join(f"- {term}.{s}.{k} ({v.get('unit')}): {v.get('hint')}"
                       for s, sv in tdef["sub_terms"].items() for k, v in (sv.get("leaves") or {}).items())
    inv = "\n".join(f"- {d['doc_id']} | {d['doc_date']} | {d['doc_type']} | {d['title']} | terms: {', '.join(d.get('terms_covered') or [])}"
                    f"{' | effective ' + d['effective_date_in_text'] if d.get('effective_date_in_text') else ''}"
                    f"{' | scope: ' + d['doc_scope_note'] if d.get('doc_scope_note') else ''}" for d in inventory)
    system = tmpl.format(term=term, description=tdef["description"], trader_id=trader_id, leaves=leaves or "(none known)", inventory=inv)
    agent = f"researcher:{term}"
    tools = researcher_tools(trader_id, agent)

    def offline(box: ToolBox) -> str:
        from .offline_rules import HEADING_TO_LEAF
        my_leaves = {k for s, sv in tdef["sub_terms"].items() for k in (sv.get("leaves") or {})}
        outlines = box.call("get_section_outlines", doc_ids=[d["doc_id"] for d in inventory])
        choices = []
        for o in outlines:
            for s in o.get("outline", []):
                by_tag = any(t.startswith(term + ".") for t in s["term_tags"])
                by_heading = HEADING_TO_LEAF.get((s["heading"] or "").strip().lower()) in my_leaves
                if by_tag or by_heading:
                    choices.append({"doc_id": o["doc_id"], "section_id": s["section_id"],
                                    "reason": ("tagged " + ", ".join(t for t in s["term_tags"] if t.startswith(term))) if by_tag
                                    else f"heading '{s['heading']}' is a {term} clause"})
        chosen = {c["section_id"] for c in choices}
        keywords = " ".join({w for s, sv in tdef["sub_terms"].items() for k, v in (sv.get("leaves") or {}).items()
                             for w in (v.get("hint") or "").split()})
        backstop = [h for h in box.call("search_sections", query=keywords, top_k=12) if h["section_id"] not in chosen] if keywords.strip() else []
        secs = box.call("read_chosen_sections", choices=choices) if choices else []
        for h in backstop:
            secs.append(box.call("read_section", doc_id=h["doc_id"], section_id=h["section_id"]))
        n = 0
        for sec in secs:
            if "error" in sec:
                continue
            sid, h = sec["section_id"], {"doc_id": sec["doc"]["doc_id"]}
            eff = sec.get("effective_date")
            eff = eff if eff and eff != sec["doc"]["doc_date"] else None
            for f in extract_values(sec["text"], sec["heading"], term):
                if f["leaf"] not in PATTERNS and f["assertion"] == "set":
                    continue
                scope = next((d.get("doc_scope_note") for d in inventory if d["doc_id"] == h["doc_id"]), None)
                r = box.call("record_finding", path=f["path"], value=f["value"], quote=f["quote"], doc_id=h["doc_id"],
                             section_id=sid, assertion=f["assertion"], effective_date=eff, scope_note=scope)
                n += int(bool(r.get("recorded")))
        return (f"Offline researcher for {term}: chose {len(choices)} sections from the outlines, read {len(backstop)} "
                f"extra from search, recorded {n} findings.")

    res = llm.run_agent(name=agent, system=system, tools=tools, offline_policy=offline, prompt_version=version,
                        user=f"Research {term} for trader {trader_id} across all documents and record every finding.")
    recorded = sum(1 for c in res.tool_calls if c["tool"] == "record_finding" and not c["error"])
    return {"term": term, "summary": res.final_text, "steps": res.steps, "tool_calls": len(res.tool_calls), "recorded": recorded,
            "tools_used": json.dumps(sorted({c["tool"] for c in res.tool_calls}))}
