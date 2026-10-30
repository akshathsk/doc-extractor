"""Extraction graph (LangGraph).

    orchestrator ──Send×N──► researcher(term) ──► reconciler ──► human_review? ──► summarize_publish
                                                         (interrupt when conflicts remain)

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
from .agents.researcher import run_researcher
from .agents.summarizer import summarize
from .config import settings
from .db import audit, j, tx
from .observability import in_run, span
from .schemas import TraderExtract, taxonomy
from .tools.docs import list_trader_docs
from .tools.extracts import publish_extract
from .tools.reconcile import conflict_payload, open_conflicts
from .tools.search import search_sections


class ExtractState(TypedDict, total=False):
    run_id: str
    trader_id: str
    caller: str
    inventory: list[dict]
    terms: list[str]
    term: str  # set on Send to a researcher
    reports: Annotated[list[dict], operator.add]
    resolutions: dict[str, dict]
    extract: dict
    review: dict
    published_version: int


def _node(name: str):
    def deco(fn):
        def wrapper(state: ExtractState) -> dict:
            with in_run(state["run_id"]), span("node", name, input={k: v for k, v in state.items()
                                                                       if k in ("trader_id", "term", "terms", "resolutions")}) as s:
                out = fn(state)
                s.output = out
                return out
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


@_node("orchestrator")
def orchestrator(state: ExtractState) -> dict:
    """Builds the document inventory and picks which term researchers to start.
    A term is researched if any document's enrichment covers it, or keyword search finds it (tag-miss backstop)."""
    tid = state["trader_id"]
    with span("tool", "list_trader_docs", input={"trader_id": tid}) as s:
        inv = list_trader_docs(tid, state.get("caller", "system"))
        s.output = inv
    if not inv:
        raise ValueError(f"no documents registered for {tid}")
    covered = {t for d in inv for t in (d.get("terms_covered") or [])}
    terms = []
    for term, tdef in taxonomy().items():
        if term == "Other":
            continue
        if term in covered:
            terms.append(term)
            continue
        hint = " ".join(v.get("hint", "") for sv in tdef["sub_terms"].values() for v in (sv.get("leaves") or {}).values())
        with span("tool", "search_sections", input={"query": hint, "trader_id": tid}) as s:
            hits = search_sections(hint, tid, top_k=3)
            s.output = hits
        if hits and hits[0]["score"] > 6:
            terms.append(term)
    return {"inventory": inv, "terms": terms}


def fan_out(state: ExtractState) -> list[Send]:
    return [Send("researcher", {"run_id": state["run_id"], "trader_id": state["trader_id"], "term": t,
                                "inventory": state["inventory"]}) for t in state["terms"]]


@_node("researcher")
def researcher(state: ExtractState) -> dict:
    return {"reports": [run_researcher(state["trader_id"], state["term"], state["inventory"])]}


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
    g.add_node("researcher", researcher)
    g.add_node("reconciler", reconciler)
    g.add_node("human_review", human_review)
    g.add_node("summarize_publish", summarize_publish)
    g.add_edge(START, "orchestrator")
    g.add_conditional_edges("orchestrator", fan_out, ["researcher"])
    g.add_edge("researcher", "reconciler")
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
