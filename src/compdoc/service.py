"""Shared core used by CLI, API, MCP server and both UIs."""
from __future__ import annotations

from typing import Any

from langgraph.types import Command

from .db import q, q1
from .graph import compiled
from .ingest import ingest_document, register_from_manifest
from .observability import set_run_status, start_run
from .schemas import TraderExtract
from .tools.access import require, seed_entitlements
from .tools.extracts import latest_extract


def ingest_all(enrich: bool = True, force: bool = False, doc_ids: list[str] | None = None, source_dir=None) -> list[dict]:
    import json
    from pathlib import Path

    from .config import settings
    src = Path(source_dir) if source_dir else settings.source_dir
    n = register_from_manifest(src)
    registered = {r["doc_id"] for r in json.loads((src / "manifest.json").read_text())}
    doc_ids = [d for d in (doc_ids or sorted(registered)) if d in registered]  # only this source's documents
    seed_entitlements()
    out = []
    with start_run("ingest", input={"registered": n, "force": force}) as run_id:
        for d in q("SELECT doc_id FROM documents ORDER BY doc_id"):
            if doc_ids and d["doc_id"] not in doc_ids:
                continue
            out.append(ingest_document(d["doc_id"], enrich=enrich, force=force))
        set_run_status(run_id, "ok", {"documents": len(out)})
    return out


def _result(run_id: str, res: dict) -> dict:
    if "__interrupt__" in res:
        payload = res["__interrupt__"][0].value
        set_run_status(run_id, "interrupted", {"pending_review": len(payload["items"])})
        return {"run_id": run_id, "status": "pending_review", "review": payload,
                "extract": res.get("extract")}
    set_run_status(run_id, "ok", {"version": res.get("published_version")})
    return {"run_id": run_id, "status": "published", "version": res.get("published_version"), "extract": res.get("extract")}


def extract_trader(trader_id: str, caller: str = "system") -> dict:
    require(caller, trader_id)
    with start_run("extract", trader_id=trader_id, caller=caller, input={"trader_id": trader_id}) as run_id:
        res = compiled().invoke({"run_id": run_id, "trader_id": trader_id, "caller": caller, "reports": []},
                                {"configurable": {"thread_id": run_id}, "max_concurrency": 6})
        return _result(run_id, res)


def resume_review(run_id: str, resolutions: dict[str, dict], reviewer: str) -> dict:
    run = q1("SELECT * FROM runs WHERE run_id=?", (run_id,))
    if not run:
        raise KeyError(run_id)
    with start_run("extract", run_id=run_id):
        res = compiled().invoke(Command(resume={"resolutions": resolutions, "reviewer": reviewer}),
                                {"configurable": {"thread_id": run_id}})
        return _result(run_id, res)


def pending_reviews(trader_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM reviews WHERE status='pending'" + (" AND trader_id=?" if trader_id else "") + " ORDER BY review_id DESC"
    return q(sql, (trader_id,) if trader_id else ())


def get_extract(trader_id: str, caller: str = "system", version: int | None = None) -> TraderExtract | None:
    require(caller, trader_id)
    return latest_extract(trader_id, version)


def traders() -> list[dict]:
    return q("SELECT trader_id, max(trader_name) trader_name, count(*) docs, min(doc_date) first_doc, max(doc_date) last_doc"
             " FROM documents GROUP BY trader_id ORDER BY trader_id")


def graph_state_history(run_id: str) -> list[dict[str, Any]]:
    """Checkpoints for the Dev Console state inspector (newest first)."""
    out = []
    for snap in compiled().get_state_history({"configurable": {"thread_id": run_id}}):
        out.append({"checkpoint_id": snap.config["configurable"].get("checkpoint_id"), "next": list(snap.next),
                    "step": (snap.metadata or {}).get("step"), "source": (snap.metadata or {}).get("source"),
                    "created_at": snap.created_at, "values": snap.values,
                    "writes": (snap.metadata or {}).get("writes")})
    return out
