"""REST API. Caller identity (for entitlements) comes from the X-Caller header (mock auth).

Run: uv run uvicorn compdoc.api:app --port 8000
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import service
from .agents.chat import chat_turn
from .agents.drafter import run_drafter
from .drafting.render_docx import load_draft
from .tools.access import AccessDenied
from .tools.clauses import find_docs_with_term, get_clause_variants
from .tools.extracts import compare_versions, extract_versions

app = FastAPI(title="compdoc", description="Agentic trader compensation contract extractor")


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except AccessDenied as e:
        raise HTTPException(403, str(e)) from e
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


class ReviewIn(BaseModel):
    resolutions: dict[str, dict[str, Any]]
    reviewer: str


class ChatIn(BaseModel):
    message: str
    history: list[dict] = []


class DraftIn(BaseModel):
    trader_id: str
    request: str


@app.get("/traders")
def traders():
    return service.traders()


@app.post("/ingest")
def ingest(force: bool = False):
    return service.ingest_all(force=force)


@app.post("/traders/{trader_id}/extract")
def run_extract(trader_id: str, x_caller: str = Header("system")):
    return _guard(service.extract_trader, trader_id, x_caller)


@app.get("/traders/{trader_id}/extract")
def get_extract(trader_id: str, version: int | None = None, x_caller: str = Header("system")):
    ex = _guard(service.get_extract, trader_id, x_caller, version)
    if not ex:
        raise HTTPException(404, "no published extract")
    return ex


@app.get("/traders/{trader_id}/versions")
def versions(trader_id: str):
    return extract_versions(trader_id)


@app.get("/traders/{trader_id}/diff")
def diff(trader_id: str, a: int, b: int, x_caller: str = Header("system")):
    return _guard(compare_versions, trader_id, a, b, x_caller)


@app.get("/reviews")
def reviews(trader_id: str | None = None):
    return service.pending_reviews(trader_id)


@app.post("/reviews/{run_id}")
def decide(run_id: str, body: ReviewIn):
    return _guard(service.resume_review, run_id, body.resolutions, body.reviewer)


@app.get("/terms/{term_path}/documents")
def term_docs(term_path: str, trader_id: str | None = None, x_caller: str = Header("system")):
    return find_docs_with_term(term_path, trader_id, caller=x_caller)


@app.get("/terms/{term_path}/variants")
def term_variants(term_path: str, x_caller: str = Header("system")):
    return get_clause_variants(term_path, caller=x_caller)


@app.post("/chat")
def chat(body: ChatIn, x_caller: str = Header("system")):
    return _guard(chat_turn, body.message, x_caller, body.history)


@app.post("/drafts")
def draft(body: DraftIn, x_caller: str = Header("system")):
    return _guard(run_drafter, body.trader_id, body.request, x_caller)


@app.get("/drafts/{draft_id}/file")
def draft_file(draft_id: str):
    d = load_draft(draft_id)
    if not d:
        raise HTTPException(404, "unknown draft")
    return FileResponse(d["path"], filename=f"{draft_id}.docx")
