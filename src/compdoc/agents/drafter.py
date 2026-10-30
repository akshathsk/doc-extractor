"""Drafting Agent: builds an amendment/side letter from precedent clauses, for human review."""
from __future__ import annotations

import re

from .. import llm
from ..llm import ToolBox
from ..observability import set_run_status, start_run
from ..prompts import load
from ..schemas import known_leaves
from ..tools.access import require
from ..tools.registry import drafter_tools

# offline: request phrase -> leaf, unit
REQUEST_PATTERNS = {
    "payout_pct_net_pnl": r"payout[^0-9]{0,30}(\d+)\s*%",
    "deferral_pct": r"deferr?al[^0-9]{0,30}(\d+)\s*%",
    "vesting_years": r"(\d+)[- ]year[^.]*vest|vest[^0-9]{0,30}(\d+)\s*years?",
    "noncompete_months": r"non-?compete[^0-9]{0,30}(\d+)\s*months?",
    "notice_period_days": r"notice[^0-9]{0,30}(\d+)\s*days?",
    "clawback_lookback_years": r"clawback[^0-9]{0,30}(\d+)\s*years?",
}
NUM_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 9: "nine", 12: "twelve", 14: "fourteen",
             15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen", 20: "twenty", 25: "twenty-five",
             30: "thirty", 35: "thirty-five", 40: "forty", 60: "sixty", 90: "ninety"}


def _substitute(text: str, new: int) -> str:
    text = re.sub(r"^The [\w\s-]+ provisions of the Agreement are deleted and replaced with the following:\s*", "", text)
    word = NUM_WORDS.get(new, str(new))
    text = re.sub(r"\b[a-z-]+ percent \(\d+%\)", f"{word} percent ({new}%)", text, count=1)
    text = re.sub(r"\b[a-z-]+ \((\d+)\) (years|months|days)", lambda m: f"{word} ({new}) {m.group(2)}", text, count=1)
    return text


def draft_inner(trader_id: str, request: str, caller: str) -> dict:
    require(caller, trader_id)
    tmpl, version = load("drafter")

    def offline(box: ToolBox) -> str:
        ex = box.call("get_trader_extract")
        current = {}
        for t in ex.get("terms", []):
            for s in t["sub_terms"]:
                for lf in s["leaves"]:
                    current[f"{t['term']}.{s['sub_term']}.{lf['key']}"] = lf["value"]
        clauses = []
        for leaf, pat in REQUEST_PATTERNS.items():
            m = re.search(pat, request, re.I)
            if not m:
                continue
            new = int(next(g for g in m.groups() if g))
            path = known_leaves()[leaf]["path"]
            sub = ".".join(path.split(".")[:2])
            v = box.call("get_clause_variants", term_path=sub)
            prec = next((x for x in v["variants"] if x["canonical_text"] and re.search(r"\(\d+%?\)", x["canonical_text"])), None)
            if prec:
                text = _substitute(prec["canonical_text"], new)
                clauses.append({"heading": path.split(".")[1], "text": text, "term_path": path, "precedent_doc_id": prec["latest_doc_id"],
                                "precedent_section_id": prec["latest_section_id"], "new_language": False,
                                "old_value": current.get(path), "new_value": new})
            else:
                clauses.append({"heading": path.split(".")[1], "text": f"[{leaf} shall be {new}]", "term_path": path,
                                "new_language": True, "old_value": current.get(path), "new_value": new})
        if not clauses:
            return "I could not identify which terms to change. Try e.g. 'raise payout to 18%' or '4 year vesting'."
        r = box.call("render_docx", doc_type="side_letter" if "side letter" in request.lower() else "amendment",
                     title="Side Letter" if "side letter" in request.lower() else "Amendment (Draft)", clauses=clauses)
        return (f"Drafted {len(clauses)} clause(s) for {trader_id}: " + "; ".join(f"{c['term_path']} {c['old_value']} → {c['new_value']}" for c in clauses)
                + f". Precedent coverage {r.get('precedent_coverage')}. File: {r.get('path')} (DRAFT, pending review).")

    res = llm.run_agent(name="drafter", system=tmpl.format(trader_id=trader_id, caller=caller, request=request),
                        user=request, tools=drafter_tools(trader_id, caller, request), offline_policy=offline,
                        prompt_version=version, effort="medium")
    return {"answer": res.final_text, "tool_calls": res.tool_calls}


def run_drafter(trader_id: str, request: str, caller: str = "system") -> dict:
    with start_run("draft", trader_id=trader_id, caller=caller, input={"request": request}) as run_id:
        out = draft_inner(trader_id, request, caller)
        set_run_status(run_id, "ok", {"answer": out["answer"]})
        return {"run_id": run_id, "answer": out["answer"]}
