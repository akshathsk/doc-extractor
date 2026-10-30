"""BM25 keyword search over sections (heading path + text + tables + enrichment summary)."""
from __future__ import annotations

import json
import re
import threading

from rank_bm25 import BM25Okapi

from ..db import q, q1
from .access import allowed_traders, require

STOP = set("the a an of to and or in on for is be shall any by with as at this that from are its which such".split())
_lock = threading.Lock()
_index: dict = {"sig": None}


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP]


def _signature() -> tuple:
    r = q1("SELECT count(*) n, coalesce(sum(length(text)),0) l, coalesce(sum(length(summary)),0) m FROM sections")
    return (r["n"], r["l"], r["m"])


def _ensure() -> dict:
    sig = _signature()
    with _lock:
        if _index["sig"] != sig:
            rows = q("SELECT s.section_id, s.doc_id, s.number, s.heading, s.path_json, s.text, s.tables_json, s.summary,"
                     " d.trader_id, d.doc_date, d.title FROM sections s JOIN documents d USING(doc_id) ORDER BY d.doc_date, s.ord")
            corpus = [tokenize(" ".join(json.loads(r["path_json"])) + " " + r["text"] + " " +
                               " ".join(json.loads(r["tables_json"])) + " " + (r["summary"] or "")) for r in rows]
            _index.update(sig=sig, rows=rows, bm25=BM25Okapi(corpus) if corpus else None)
        return _index


def search_sections(query: str, trader_id: str | None = None, doc_ids: list[str] | None = None, top_k: int = 8,
                    caller: str = "system") -> list[dict]:
    if trader_id:
        require(caller, trader_id)
    allowed = allowed_traders(caller)
    idx = _ensure()
    if not idx.get("bm25"):
        return []
    scores = idx["bm25"].get_scores(tokenize(query))
    hits = []
    for r, sc in sorted(zip(idx["rows"], scores), key=lambda x: -x[1]):
        if sc <= 0:
            break
        if trader_id and r["trader_id"] != trader_id:
            continue
        if doc_ids and r["doc_id"] not in doc_ids:
            continue
        if allowed is not None and r["trader_id"] not in allowed:
            continue
        hits.append({"doc_id": r["doc_id"], "trader_id": r["trader_id"], "doc_date": r["doc_date"], "doc_title": r["title"],
                     "section_id": r["section_id"], "number": r["number"], "heading": r["heading"],
                     "snippet": r["text"][:240], "score": round(float(sc), 3)})
        if len(hits) >= top_k:
            break
    return hits
