"""Extraction graph (LangGraph).

    orchestrator ─► router ─Send×N─► doc_reader(document, term group) ─► coverage ─(gaps? one more round)─►
                    reconciler ─► human_review? ─► summarize_publish   (interrupt when conflicts remain)

State is checkpointed in var/checkpoints.db (thread_id = run_id), so a run paused for review can be resumed later,
from another process (UI/API), and inspected step by step in the Dev Console.
"""
from __future__ import annotations

import operator
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from .agents.reconciler import propose_resolutions, reconcile_run
from .agents.doc_reader import coverage_gaps, route, run_doc_reader
from .agents.summarizer import summarize
from .config import settings
from .db import audit, j, tx
from .observability import in_run, span
from .schemas import TraderExtract
from .tools.docs import list_trader_docs
from .tools.extracts import publish_extract
from .tools.reconcile import conflict_payload, open_conflicts


class ExtractState(TypedDict, total=False):
    run_id: str
    trader_id: str
    caller: str
    inventory: list[dict]
    terms: list[str]
    items: list[dict]  # work items from the router
    item: dict  # set on Send to a doc reader
    round: int
    coverage_round: int
    gaps: list[dict]
    reports: Annotated[list[dict], operator.add]
    resolutions: dict[str, dict]
    extract: dict
    review: dict
    published_version: int


def _node(name: str):
    def deco(fn):
        def wrapper(state: ExtractState) -> dict:
            inp = {k: v for k, v in state.items() if k in ("trader_id", "resolutions", "round")}
            if "item" in state:
                inp |= {"doc_id": state["item"]["doc_id"], "terms": state["item"]["terms"]}
            with in_run(state["run_id"]), span("node", name, input=inp) as s:
                out = fn(state)
                s.output = out
                return out
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


@_node("orchestrator")
def orchestrator(state: ExtractState) -> dict:
    """Document inventory for the trader."""
    tid = state["trader_id"]
    with span("tool", "list_trader_docs", input={"trader_id": tid}) as s:
        inv = list_trader_docs(tid, state.get("caller", "system"))
        s.output = inv
    if not inv:
        raise ValueError(f"no documents registered for {tid}")
    return {"inventory": inv}


@_node("router")
def router(state: ExtractState) -> dict:
    """Coverage router (code): relevant (document, term group) work items, each with a fused candidate shortlist."""
    with span("tool", "route_work_items", input={"documents": len(state["inventory"])}) as s:
        items = route(state["trader_id"], state["inventory"])
        s.output = [{"doc_id": i["doc_id"], "terms": i["terms"], "shortlist": len(i["shortlist"]), "evidence": i["evidence"]}
                    for i in items]
    return {"items": items, "coverage_round": 1}


def fan_out(state: ExtractState) -> list[Send]:
    return [Send("doc_reader", {"run_id": state["run_id"], "trader_id": state["trader_id"], "item": it, "round": 1})
            for it in state["items"]]


@_node("doc_reader")
def doc_reader(state: ExtractState) -> dict:
    return {"reports": [run_doc_reader(state["trader_id"], state["item"], state.get("round", 1))]}


@_node("coverage")
def coverage(state: ExtractState) -> dict:
    """Coverage check (code): strong evidence that no reader read and no finding covers -> one more round."""
    with span("tool", "coverage_check", input={"round": state.get("coverage_round", 1)}) as s:
        gaps = coverage_gaps(state["trader_id"], state["items"], state.get("reports", []))
        s.output = [{"doc_id": g["doc_id"], "terms": g["terms"], "reason": g["gap_reason"]} for g in gaps]
    return {"gaps": gaps, "coverage_round": state.get("coverage_round", 1) + 1}


def after_coverage(state: ExtractState):
    if state.get("gaps") and state.get("coverage_round", 2) <= 2:
        return [Send("doc_reader", {"run_id": state["run_id"], "trader_id": state["trader_id"], "item": g, "round": 2})
                for g in state["gaps"]]
    return "reconciler"


@_node("reconciler")
def reconciler(state: ExtractState) -> dict:
    tid = state["trader_id"]
    ex = reconcile_run(tid, state.get("resolutions"))
    proposed = propose_resolutions(tid, ex)
    resolutions = {**(state.get("resolutions") or {}), **{p: r for p, r in proposed.items() if r["action"] != "escalate"}}
    if resolutions != (state.get("resolutions") or {}):
        ex = reconcile_run(tid, resolutions)
    for p, r in proposed.items():
        if r["action"] == "escalate":
            for c in ex.conflicts:
                if c.path == p and not c.resolution:
                    c.note += f" | reconciler: {r['reason']}"
    return {"extract": ex.model_dump(), "resolutions": resolutions}


def needs_review(state: ExtractState) -> str:
    ex = TraderExtract.model_validate(state["extract"])
    return "human_review" if open_conflicts(ex) else "summarize_publish"


def human_review(state: ExtractState) -> dict:
    """Pause for a person. Resume with Command(resume={"resolutions": {path: {...}}, "reviewer": "name"})."""
    ex = TraderExtract.model_validate(state["extract"])
    items = [conflict_payload(c) for c in open_conflicts(ex)]
    with in_run(state["run_id"]):
        with span("node", "human_review:requested", input={"items": items}):
            with tx() as c:
                if not c.execute("SELECT 1 FROM reviews WHERE run_id=? AND status='pending'", (state["run_id"],)).fetchone():
                    c.execute("INSERT INTO reviews(run_id, thread_id, trader_id, kind, payload_json, status, created_at)"
                              " VALUES (?,?,?,?,?,?,?)", (state["run_id"], state["run_id"], state["trader_id"], "conflicts",
                                                          j(items), "pending", datetime.now(timezone.utc).isoformat()))
                c.execute("UPDATE runs SET status='interrupted' WHERE run_id=?", (state["run_id"],))
    decision = interrupt({"run_id": state["run_id"], "trader_id": state["trader_id"], "items": items})
    # --- resumed ---
    with in_run(state["run_id"]):
        with span("node", "human_review:decided", input=decision) as s:
            res = {p: {**r, "by": decision.get("reviewer", "reviewer")} for p, r in (decision.get("resolutions") or {}).items()}
            merged = {**(state.get("resolutions") or {}), **res}
            ex2 = reconcile_run(state["trader_id"], merged)
            with tx() as c:
                c.execute("UPDATE reviews SET status='decided', decision_json=?, reviewer=?, decided_at=? WHERE run_id=? AND status='pending'",
                          (j(decision), decision.get("reviewer"), datetime.now(timezone.utc).isoformat(), state["run_id"]))
            audit(decision.get("reviewer", "reviewer"), "review_decided", state["trader_id"], run_id=state["run_id"], decision=decision)
            s.output = {"resolutions": res}
    return {"resolutions": merged, "extract": ex2.model_dump(), "review": decision}


@_node("summarize_publish")
def summarize_publish(state: ExtractState) -> dict:
    ex = TraderExtract.model_validate(state["extract"])
    ex.run_id = state["run_id"]
    ex.documents = [{k: d.get(k) for k in ("doc_id", "title", "doc_type", "doc_date", "doc_summary")} for d in state["inventory"]]
    ex.summary = summarize(ex)
    with span("tool", "publish_extract", input={"trader_id": ex.trader_id}) as s:
        publish_extract(ex)
        s.output = {"version": ex.version}
    audit("system", "extract_published", ex.trader_id, run_id=state["run_id"], version=ex.version)
    return {"extract": ex.model_dump(), "published_version": ex.version}


def build_graph() -> StateGraph:
    g = StateGraph(ExtractState)
    g.add_node("orchestrator", orchestrator)
    g.add_node("router", router)
    g.add_node("doc_reader", doc_reader)
    g.add_node("coverage", coverage)
    g.add_node("reconciler", reconciler)
    g.add_node("human_review", human_review)
    g.add_node("summarize_publish", summarize_publish)
    g.add_edge(START, "orchestrator")
    g.add_edge("orchestrator", "router")
    g.add_conditional_edges("router", fan_out, ["doc_reader"])
    g.add_edge("doc_reader", "coverage")
    g.add_conditional_edges("coverage", after_coverage, ["doc_reader", "reconciler"])
    g.add_conditional_edges("reconciler", needs_review, ["human_review", "summarize_publish"])
    g.add_edge("human_review", "summarize_publish")
    g.add_edge("summarize_publish", END)
    return g


@lru_cache(maxsize=1)
def checkpointer() -> SqliteSaver:
    conn = sqlite3.connect(settings.checkpoint_path, check_same_thread=False)
    return SqliteSaver(conn)


@lru_cache(maxsize=1)
def compiled():
    return build_graph().compile(checkpointer=checkpointer())


def graph_for_studio() -> Any:
    """Entry point for `langgraph dev` (LangGraph Studio supplies its own checkpointer)."""
    return build_graph().compile()
