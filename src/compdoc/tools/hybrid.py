"""Hybrid retrieval (scaling plan, Part 3 / R4): term tags + keyword (BM25) + semantic (local embeddings), fused.

- Embeddings come from a LOCAL model (default sentence-transformers/all-mpnet-base-v2): contract text never leaves.
- Vectors are cached per section content hash + model, long sections split into ~1,500-character chunks.
- Fusion is reciprocal rank fusion (RRF); every candidate records which signal found it (provenance for the Dev Console).
- Semantic hits only POINT to sections; values still come from reading the text with verified quotes.
Production note: the SQLite + numpy store here stands in for pgvector / OpenSearch k-NN, filtered by trader.
"""
from __future__ import annotations

import os
import threading
from functools import lru_cache

import numpy as np

from ..db import q, tx
from ..schemas import taxonomy
from .access import allowed_traders, require
from .search import search_sections

MODEL = os.environ.get("COMPDOC_EMBED_MODEL", "sentence-transformers/all-mpnet-base-v2")
CHUNK = 1500
RRF_K = 60
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS section_vectors (
  section_hash TEXT, model TEXT, chunk INTEGER, dim INTEGER, vec BLOB, PRIMARY KEY (section_hash, model, chunk));
"""


@lru_cache(maxsize=1)
def model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(MODEL)


def _ensure_schema() -> None:
    with tx() as c:
        c.executescript(SCHEMA)


def _chunks(heading_path: str, text: str) -> list[str]:
    body = text or ""
    parts = [body[i:i + CHUNK] for i in range(0, max(len(body), 1), CHUNK - 200)] or [""]
    return [f"{heading_path}\n{p}".strip() for p in parts]


def embed_doc(doc_id: str) -> dict:
    """Embed sections whose content hash has no vectors yet for this model (unchanged sections are skipped)."""
    _ensure_schema()
    rows = q("SELECT s.section_id, s.path_json, s.text, t.section_hash FROM sections s JOIN section_text t USING(section_id) "
             "WHERE s.doc_id=? AND s.level > 0", (doc_id,))
    have = {r["section_hash"] for r in q("SELECT DISTINCT v.section_hash FROM section_vectors v JOIN section_text t "
                                          "ON t.section_hash=v.section_hash WHERE t.doc_id=? AND v.model=?", (doc_id, MODEL))}
    todo = [r for r in rows if r["section_hash"] not in have]
    texts, keys = [], []
    for r in todo:
        import json
        for i, ch in enumerate(_chunks(" > ".join(json.loads(r["path_json"])), r["text"])):
            texts.append(ch)
            keys.append((r["section_hash"], i))
    if texts:
        with _lock:
            vecs = model().encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False).astype(np.float32)
        with tx() as c:
            c.executemany("INSERT OR REPLACE INTO section_vectors(section_hash, model, chunk, dim, vec) VALUES (?,?,?,?,?)",
                          [(h, MODEL, i, vecs.shape[1], v.tobytes()) for (h, i), v in zip(keys, vecs)])
        _matrix.cache_clear()
    return {"sections": len(rows), "embedded": len(todo), "reused": len(rows) - len(todo), "chunks": len(texts)}


@lru_cache(maxsize=32)
def _matrix(scope_key: str) -> tuple[list[str], np.ndarray]:
    """(section_ids per chunk row, matrix) for a scope 'trader:T-1' or 'doc:D-1' or 'all'."""
    _ensure_schema()
    kind, _, val = scope_key.partition(":")
    where, params = ("", ()) if kind == "all" else (("AND d.trader_id=?", (val,)) if kind == "trader" else ("AND s.doc_id=?", (val,)))
    rows = q("SELECT s.section_id, v.vec FROM sections s JOIN section_text t USING(section_id) JOIN documents d ON d.doc_id=s.doc_id "
             f"JOIN section_vectors v ON v.section_hash=t.section_hash AND v.model=? WHERE s.level > 0 {where}", (MODEL, *params))
    if not rows:
        return [], np.zeros((0, 1), dtype=np.float32)
    return [r["section_id"] for r in rows], np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])


@lru_cache(maxsize=512)
def _encode_query(text: str) -> np.ndarray:
    """Query embeddings are cached: the same term query is reused across every document of every trader."""
    with _lock:
        return model().encode([text], normalize_embeddings=True, show_progress_bar=False)[0].astype(np.float32)


def semantic_search(query: str, trader_id: str | None = None, doc_ids: list[str] | None = None, top_k: int = 20) -> list[dict]:
    scopes = [f"doc:{d}" for d in doc_ids] if doc_ids else [f"trader:{trader_id}" if trader_id else "all"]
    qv = _encode_query(query)
    best: dict[str, float] = {}
    for sc in scopes:
        ids, m = _matrix(sc)
        if not ids:
            continue
        sims = m @ qv
        for sid, s in zip(ids, sims):
            if s > best.get(sid, -1):
                best[sid] = float(s)
    return [{"section_id": k, "score": round(v, 4)} for k, v in sorted(best.items(), key=lambda kv: -kv[1])[:top_k]]


def term_query(term_path: str) -> str:
    """Natural-language query for a taxonomy term or sub-term (used for keyword and semantic search)."""
    parts = term_path.split(".")
    t = taxonomy().get(parts[0], {})
    subs = t.get("sub_terms", {})
    chosen = {parts[1]: subs[parts[1]]} if len(parts) > 1 and parts[1] in subs else subs
    hints = [v.get("hint", "") for sv in chosen.values() for v in (sv.get("leaves") or {}).values()]
    names = " ".join(chosen)
    return f"{parts[0]} {names}: {t.get('description', '')} " + "; ".join(hints)


def hybrid_search(term_path: str | None = None, query: str | None = None, trader_id: str | None = None,
                  doc_ids: list[str] | None = None, top_k: int = 10, caller: str = "system", pool: int = 40) -> list[dict]:
    """Fused candidates with per-signal ranks: {'tag': bool, 'bm25': rank|None, 'semantic': rank|None}."""
    if trader_id:
        require(caller, trader_id)
    allowed = allowed_traders(caller)
    text = query or term_query(term_path or "")
    ranks: dict[str, dict] = {}

    def add(sid: str, signal: str, rank: int) -> None:
        ranks.setdefault(sid, {"tag": None, "bm25": None, "semantic": None})[signal] = rank
    if term_path:
        sql = ("SELECT s.section_id, s.term_tags_json FROM sections s JOIN documents d USING(doc_id) WHERE s.level > 0"
               + (" AND d.trader_id=?" if trader_id else "") + (f" AND s.doc_id IN ({','.join('?' * len(doc_ids))})" if doc_ids else "")
               + " ORDER BY d.doc_date, s.ord")
        params = ((trader_id,) if trader_id else ()) + tuple(doc_ids or ())
        n = 0
        for r in q(sql, params):
            tags = __import__("json").loads(r["term_tags_json"] or "[]")
            if any(t == term_path or t.startswith(term_path + ".") for t in tags):
                n += 1
                add(r["section_id"], "tag", 1)  # tags are unordered: all tagged sections share the top rank
    for i, h in enumerate(search_sections(text, trader_id, doc_ids, top_k=pool, caller=caller), 1):
        add(h["section_id"], "bm25", i)
    sem_scores: dict[str, float] = {}
    for i, h in enumerate(semantic_search(text, trader_id, doc_ids, top_k=pool), 1):
        add(h["section_id"], "semantic", i)
        sem_scores[h["section_id"]] = h["score"]
    scored = []
    for sid, r in ranks.items():
        s = sum(1 / (RRF_K + rk) for rk in r.values() if rk)
        scored.append((s, sid, r))
    scored.sort(key=lambda x: -x[0])
    out = []
    ids = [sid for _, sid, _ in scored[:top_k]]
    meta = {m["section_id"]: m for m in q(
        f"SELECT s.section_id, s.doc_id, s.number, s.heading, s.summary, substr(s.text,1,200) snippet, d.trader_id, d.doc_date "
        f"FROM sections s JOIN documents d USING(doc_id) WHERE s.section_id IN ({','.join('?' * len(ids))})", tuple(ids))} if ids else {}
    for s, sid, r in scored[:top_k]:
        m = meta.get(sid, {})
        if allowed is not None and m.get("trader_id") not in allowed:
            continue
        out.append({**m, "score": round(s, 5), "found_by": [k for k, v in r.items() if v], "ranks": r,
                    "semantic_score": sem_scores.get(sid)})
    return out
