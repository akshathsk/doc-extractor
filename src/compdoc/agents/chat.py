"""Chat: one entry point, a cheap router, three skills (trader Q&A, term finder, drafting)."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from .. import llm
from ..llm import ToolBox
from ..observability import set_run_status, span, start_run
from ..tools.citations import build_citations
from ..prompts import load
from ..schemas import known_leaves, sub_term_paths
from ..tools.registry import finder_tools, qa_tools
from .digitizer import KEYWORDS


class Route(BaseModel):
    route: Literal["trader_qa", "term_finder", "drafting"]
    trader_id: str | None = None
    term_path: str | None = None


def _guess_term(text: str) -> str | None:
    low = text.lower()
    best = None
    for path, kws in KEYWORDS.items():
        if any(k.strip() in low for k in kws) or path.split(".")[-1].lower() in low:
            best = best or path
    for word, path in (("clawback", "Clawback"), ("guarantee", "Guarantee"), ("non-compete", "RestrictiveCovenants.NonCompete"),
                       ("noncompete", "RestrictiveCovenants.NonCompete"), ("deferral", "Deferral"), ("vesting", "Deferral.Vesting"),
                       ("payout", "Compensation.Payout"), ("notice", "Termination.Notice"), ("draw", "Guarantee.Draw")):
        if word in low:
            return path
    return best


def route(message: str) -> Route:
    tmpl, version = load("chat_router")
    tid = re.search(r"\bT-\d{4}\b", message)

    def offline() -> Route:
        low = message.lower()
        r = "drafting" if re.search(r"\b(draft|prepare|write up|generate)\b", low) else \
            "term_finder" if re.search(r"which (docs|documents|traders)|who has|latest .* (language|wording)|find .* clause|across", low) \
            else "trader_qa"
        return Route(route=r, trader_id=tid.group(0) if tid else None, term_path=_guess_term(message))

    r = llm.structured(name="chat_router", system=tmpl + "\nAlso extract trader_id (format T-1234) and a taxonomy term_path if present.",
                       user=message, schema=Route, offline=offline, prompt_version=version)
    if tid and not r.trader_id:
        r.trader_id = tid.group(0)
    return r


def _fmt_leaf(path: str, lf: dict) -> str:
    s = lf["source"]
    hist = "; previously " + ", ".join(f"{h['value']} ({h['doc_id']}, {h['effective_date']})" for h in lf["history"]) if lf["history"] else ""
    return f"- {path} = {lf['value']} {lf.get('unit') or ''} [{lf['status']}] [{s['doc_id']}, {s.get('effective_date')}, {s['section_id'].split(':')[-1]}]{hist}"


def trader_qa(message: str, caller: str, trader_id: str | None, history: list[dict]) -> llm.AgentResult:
    tmpl, version = load("trader_qa")

    def offline(box: ToolBox) -> str:
        if not trader_id:
            return "Which trader? Please include a trader id like T-1003."
        term = _guess_term(message)
        if term:
            res = box.call("get_term", trader_id=trader_id, path=term)
            if "leaves" in res:
                return f"{trader_id} {term} (extract v{res['version']}):\n" + "\n".join(_fmt_leaf(p, lf) for p, lf in res["leaves"].items())
        ex = box.call("get_trader_extract", trader_id=trader_id)
        if "error" in ex:
            hits = box.call("search_sections", query=message, trader_id=trader_id, top_k=3)
            return ex["error"] + "\nClosest sections:\n" + "\n".join(f"- [{h['doc_id']}, {h['doc_date']}, {h['number']}] {h['snippet']}" for h in hits)
        return ex.get("summary") or "No summary available."

    res = llm.run_agent(name="trader_qa", system=tmpl.format(caller=caller), user=message, tools=qa_tools(caller),
                        offline_policy=offline, prompt_version=version, history=history)
    return res


def term_finder(message: str, caller: str, term_path: str | None, history: list[dict]) -> llm.AgentResult:
    tmpl, version = load("term_finder")

    def offline(box: ToolBox) -> str:
        if not term_path:
            return "Which term? e.g. clawback, deferral, guarantee, payout."
        res = box.call("find_docs_with_term", term_path=term_path)
        lines = [f"{res['count']} sections in {res['documents']} documents contain {term_path} (newest first):"]
        for r in res["results"][:15]:
            lines.append(f"- {r['doc_id']} | {r['trader_id']} | {r['doc_date']} | {r['number']} {r['heading']} | "
                         f"{(r['summary'] or '[not entitled - text withheld]')[:110]}")
        if "latest" in message.lower() or "language" in message.lower() or "wording" in message.lower():
            v = box.call("get_clause_variants", term_path=term_path)
            if v["variants"]:
                top = v["variants"][0]
                lines.append(f"\nLatest wording ({top['variant_id']}, {top['latest_doc_id']}, {top['latest_doc_date']}, used in "
                             f"{top['doc_count']} docs):\n> {top['canonical_text'] or '[withheld]'}")
                lines.append(f"{len(v['variants'])} distinct variants in total.")
        return "\n".join(lines)

    res = llm.run_agent(name="term_finder", system=tmpl.format(caller=caller, paths=", ".join(sub_term_paths())),
                        user=message, tools=finder_tools(caller), offline_policy=offline, prompt_version=version, history=history)
    return res


def chat_turn(message: str, caller: str = "system", history: list[dict] | None = None, run_id: str | None = None) -> dict:
    """run_id may be supplied up front so a UI can watch the trace while the turn is running."""
    history = history or []
    with start_run("chat", caller=caller, input={"message": message}, run_id=run_id) as run_id:
        r = route(message)
        calls: list[dict] = []
        if r.route == "drafting":
            from .drafter import draft_inner
            if not r.trader_id:
                answer = "Which trader should the draft be for? Include a trader id like T-1003."
            else:
                d = draft_inner(r.trader_id, message, caller)
                answer, calls = d["answer"], d["tool_calls"]
        else:
            res = term_finder(message, caller, r.term_path, history) if r.route == "term_finder" else \
                trader_qa(message, caller, r.trader_id, history)
            answer, calls = res.final_text, res.tool_calls
        with span("tool", "build_citations", input={"tool_calls": len(calls)}) as s:
            citations = build_citations(answer, calls, caller)
            s.output = citations
        set_run_status(run_id, "ok", {"route": r.route, "trader_id": r.trader_id, "answer": answer[:2000], "citations": citations})
        if r.trader_id:  # lets the Dev Console find questions asked about a trader
            from ..db import tx
            with tx() as c:
                c.execute("UPDATE runs SET trader_id=? WHERE run_id=?", (r.trader_id, run_id))
        return {"run_id": run_id, "route": r.route, "trader_id": r.trader_id, "answer": answer, "citations": citations}


__all__ = ["chat_turn", "route", "known_leaves"]
