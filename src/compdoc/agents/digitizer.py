"""Digitization Agent - enrichment at scale (scaling plan, Part 2).

  map     sections -> token-sized batches -> parallel LLM calls (shared rate limiter, retries, completeness check)
          each batch returns per-section fields + document signals (effective date / period limit / purpose, cited)
  cache   per section, keyed by content hash + prompt version + model: unchanged sections are never re-enriched
  reduce  1) per article: summary from its sections' summaries   2) per document: summary, effective date, scope
          (effective date and scope must cite a section; the quote is verified against the text store)
  switch  section columns, article summaries and the document row are written in one transaction at the end,
          so agents never see a half-old, half-new document

Output is navigation metadata only - never evidence (findings must quote raw section text).
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from .. import llm
from ..config import settings
from ..db import j, q, q1, tx
from ..ingest import load_sections
from ..observability import span
from ..prompts import load
from ..schemas import SectionEnrichment, is_period_limit, sub_term_paths, taxonomy

BATCH_TOKENS = int(os.environ.get("COMPDOC_ENRICH_BATCH_TOKENS", "8000"))
CONCURRENCY = int(os.environ.get("COMPDOC_ENRICH_CONCURRENCY", "8"))
RPM = int(os.environ.get("COMPDOC_LLM_RPM", "300"))
TPM = int(os.environ.get("COMPDOC_LLM_TPM", "400000"))
ARTICLE_MIN_CHILDREN = 3

# Offline keyword tagger (also a recall backstop in live mode).
KEYWORDS = {
    "Compensation.Base": ["base salary"],
    "Compensation.Payout": ["payout equal to", "payout shall be", "percent", "of net trading p&l"],
    "Compensation.Hurdle": ["hurdle"],
    "Compensation.HighWaterMark": ["high-water mark", "high water mark"],
    "Compensation.Netting": ["netted", "netting"],
    "Deferral.Schedule": ["deferred", "defer ", "deferral threshold"],
    "Deferral.Vesting": ["vest"],
    "Clawback.Terms": ["clawback", "recover payouts"],
    "Guarantee.Guarantee": ["guarantee", "no less than"],
    "Guarantee.Draw": ["draw"],
    "RestrictiveCovenants.NonCompete": ["competing business", "non-compet"],
    "RestrictiveCovenants.NonSolicit": ["solicit"],
    "Termination.Notice": ["notice", "garden leave"],
}


def _offline_tags(heading: str, text: str) -> list[str]:
    hay = f"{heading}\n{text}".lower()
    tags = [p for p, kws in KEYWORDS.items() if any(re.search(r"\b" + re.escape(k.strip()), hay) for k in kws)]
    if "Compensation.Payout" in tags and ("defer" in hay or "guarantee" in hay or "hurdle" in hay):
        if not re.search(r"payout (equal to|shall be)|% ?\) of net trading", hay):
            tags.remove("Compensation.Payout")
    if "solicit" in hay and "Compensation.Payout" in tags:
        tags.remove("Compensation.Payout")
    return tags


# ------------------------------------------------------------------------------------------ structured outputs
class DocSignal(BaseModel):
    kind: Literal["effective_date", "period_limit", "purpose"]
    value: str
    section_id: str
    quote: str = Field(description="exact words from the section")


class BatchEnrichment(BaseModel):
    sections: list[SectionEnrichment]
    signals: list[DocSignal] = []


class ArticleSummary(BaseModel):
    summary: str


class CitedValue(BaseModel):
    value: str
    section_id: str
    quote: str


class DocFields(BaseModel):
    doc_summary: str
    effective_date_in_text: CitedValue | None = None
    doc_scope_note: CitedValue | None = None


# ------------------------------------------------------------------------------------------ rate limiter
class RateLimiter:
    """Shared across threads: requests and tokens per minute (sliding 60 s window)."""

    def __init__(self, rpm: int, tpm: int):
        self.rpm, self.tpm, self.lock, self.events = rpm, tpm, threading.Lock(), []

    def acquire(self, tokens: int) -> None:
        while True:
            with self.lock:
                now = time.time()
                self.events = [(t, n) for t, n in self.events if now - t < 60]
                if len(self.events) < self.rpm and sum(n for _, n in self.events) + tokens <= self.tpm:
                    self.events.append((now, tokens))
                    return
            time.sleep(0.2)


limiter = RateLimiter(RPM, TPM)


def est_tokens(s: str) -> int:
    return len(s) // 4 + 1


# ------------------------------------------------------------------------------------------ offline fallbacks
def _offline_section(s) -> SectionEnrichment:
    text = s.text
    scope_notes = re.findall(r"[^.]*\b(?:only|effective \d{4}-\d{2}-\d{2}|deleted in (?:their|its) entirety)[^.]*\.", text)
    return SectionEnrichment(
        section_id=s.section_id,
        summary=(text.split(". ")[0][:220] + ("." if text else "")) if text else f"Heading: {s.heading}",
        term_tags=[] if s.level == 0 else _offline_tags(s.heading, text),
        defined_terms=re.findall(r'"([A-Z][^"]{2,40})"(?: means|\))', text),
        cross_refs=re.findall(r"\b(?:clause|section)\s+(\d+(?:\.\d+)*)", text, re.I),
        scope_notes=[x.strip() for x in scope_notes])


def _offline_signals(sections) -> list[DocSignal]:
    out = []
    for s in sections:
        m = re.search(r"[^.]*\beffective (\d{4}-\d{2}-\d{2})[^.]*\.?", s.text)
        if m:
            out.append(DocSignal(kind="effective_date", value=m.group(1), section_id=s.section_id, quote=m.group(0).strip()))
        m2 = re.search(r"[^.]*apply for the Performance Year \d{4} only\.?", s.text)
        if m2:
            out.append(DocSignal(kind="period_limit", value=m2.group(0).strip(), section_id=s.section_id, quote=m2.group(0).strip()))
    return out


# ------------------------------------------------------------------------------------------ map
def _pack(sections, budget: int = BATCH_TOKENS) -> list[list]:
    """Batches by estimated tokens, in document order (siblings stay together)."""
    batches, cur, size = [], [], 0
    for s in sections:
        t = est_tokens(s.text + " ".join(s.tables_md)) + 40
        if cur and size + t > budget:
            batches.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += t
    if cur:
        batches.append(cur)
    return batches


def _map_batch(doc: dict, batch: list, system: str, version: str, label: str) -> BatchEnrichment:
    payload = {"document": {k: doc[k] for k in ("doc_id", "trader_id", "title", "doc_type", "doc_date")},
               "sections": [{"section_id": s.section_id, "number": s.number, "heading": s.heading, "path": s.path,
                             "text": s.text[:24000], "tables": s.tables_md} for s in batch]}
    user = json.dumps(payload)
    limiter.acquire(est_tokens(system + user) + 1500)
    return llm.structured(name=f"digitizer:{doc['doc_id']}:{label}", system=system, user=user, schema=BatchEnrichment,
                          offline=lambda: BatchEnrichment(sections=[_offline_section(s) for s in batch],
                                                          signals=_offline_signals(batch)),
                          prompt_version=version)


def _run_parallel(fn, items: list) -> list:
    """Thread pool that keeps the tracing context (spans land under the current run)."""
    with ThreadPoolExecutor(max_workers=max(1, min(CONCURRENCY, len(items)))) as ex:
        futs = [ex.submit(contextvars.copy_context().run, fn, it) for it in items]
        return [f.result() for f in futs]


# ------------------------------------------------------------------------------------------ main entry
def enrich_document(doc_id: str, force: bool = False) -> dict:
    tmpl, version = load("digitizer")
    _, a_version = load("digitizer_article")
    _, d_version = load("digitizer_doc")
    model = settings.fast_model if settings.provider != "offline" else "offline"
    full_version = f"{version}+{a_version}+{d_version}"
    doc = q1("SELECT * FROM documents WHERE doc_id=?", (doc_id,))
    sections = load_sections(doc_id)
    hashes = {r["section_id"]: r["section_hash"] for r in q("SELECT section_id, section_hash FROM section_text WHERE doc_id=?", (doc_id,))}
    for s in sections:
        hashes.setdefault(s.section_id, s.content_hash())
    content_hash = hashlib.sha1("|".join(hashes[s.section_id] for s in sections).encode()).hexdigest()[:20]
    cached_doc = q1("SELECT * FROM doc_enrichment WHERE doc_id=?", (doc_id,))
    if (cached_doc and cached_doc["prompt_version"] == full_version and cached_doc["model"] == model
            and cached_doc["content_hash"] == content_hash and not force):
        return {"cached": True}
    allowed = set(sub_term_paths())
    system = tmpl.format(allowed="\n".join(f"- {p}" for p in sorted(allowed)))

    # ---- cache lookup per section
    cache: dict[str, dict] = {}
    if not force:
        for s in sections:
            r = q1("SELECT * FROM section_enrichment WHERE section_hash=? AND prompt_version=? AND model=?",
                   (hashes[s.section_id], version, model))
            if r:
                cache[s.section_id] = r
    todo = [s for s in sections if s.section_id not in cache]
    batches = _pack(todo)

    # ---- map: parallel batches, retry, completeness check
    results: dict[str, SectionEnrichment] = {}
    signals: list[DocSignal] = []
    retried = 0
    with span("node", f"enrich-map:{doc_id}", input={"sections": len(sections), "cached": len(cache), "batches": len(batches)}) as sp:
        def run_batch(arg):
            i, batch = arg
            for attempt in range(3):
                try:
                    return batch, _map_batch(doc, batch, system, version, f"b{i + 1}"), None
                except Exception as e:  # noqa: BLE001 - transient API errors: retry, then report
                    err = f"{type(e).__name__}: {e}"
                    time.sleep(1.5 * (attempt + 1))
            return batch, None, err
        for batch, res, err in _run_parallel(run_batch, list(enumerate(batches))):
            got = {e.section_id: e for e in (res.sections if res else []) if e.section_id in {s.section_id for s in batch}}
            missing = [s for s in batch if s.section_id not in got]
            if missing:  # completeness: retry the missing sections once, alone
                retried += len(missing)
                _, res2, _ = run_batch((len(batches) + len(results), missing))
                for e in (res2.sections if res2 else []):
                    got.setdefault(e.section_id, e)
                for s in missing:  # still missing: offline fallback, flagged
                    got.setdefault(s.section_id, _offline_section(s))
            results.update(got)
            signals += [x for x in (res.signals if res else []) if x.section_id in {s.section_id for s in batch}]
        sp.output = {"batches": len(batches), "enriched": len(results), "reused": len(cache), "retried_sections": retried,
                     "signals": len(signals)}

    # ---- guardrails: tags must exist in the taxonomy
    bad_tags = 0
    for e in results.values():
        tags = [t for t in e.term_tags if t in allowed]
        bad_tags += len(e.term_tags) - len(tags)
        e.term_tags = tags
    merged: dict[str, dict] = {}
    for sid, r in cache.items():
        merged[sid] = {"summary": r["summary"], "term_tags": json.loads(r["term_tags_json"] or "[]"),
                       "defined_terms": json.loads(r["defined_terms_json"] or "[]"), "cross_refs": json.loads(r["cross_refs_json"] or "[]"),
                       "scope_notes": json.loads(r["scope_notes_json"] or "[]")}
        signals += [DocSignal(**x) for x in json.loads(r["signals_json"] or "[]")]
    for sid, e in results.items():
        merged[sid] = e.model_dump(exclude={"section_id"})

    # ---- reduce 1: article summaries (top-level parts with several sections)
    by_id = {s.section_id: s for s in sections}
    children: dict[str, list] = {}
    for s in sections:
        if s.parent_id:
            children.setdefault(s.parent_id, []).append(s)
    def descendants(sid):
        out = []
        for c in children.get(sid, []):
            out.append(c)
            out += descendants(c.section_id)
        return out
    articles = [s for s in sections if s.level == 1 and len(descendants(s.section_id)) >= ARTICLE_MIN_CHILDREN]
    a_tmpl, _ = load("digitizer_article")

    def reduce_article(a):
        kids = descendants(a.section_id)
        child_hash = hashlib.sha1("|".join(hashes[k.section_id] for k in kids).encode()).hexdigest()[:20]
        r = q1("SELECT summary FROM article_enrichment WHERE section_id=? AND child_hash=? AND prompt_version=? AND model=?",
               (a.section_id, child_hash, a_version, model))
        if r and not force:
            return a.section_id, r["summary"], child_hash, True
        lines = [f"{k.number or ''} {k.heading}: {merged.get(k.section_id, {}).get('summary', '')}" for k in kids]
        user = json.dumps({"article": f"{a.number or ''} {a.heading}", "section_summaries": lines})
        limiter.acquire(est_tokens(user) + 300)
        res = llm.structured(name=f"digitizer-article:{a.section_id}", system=a_tmpl, user=user, schema=ArticleSummary,
                             offline=lambda: ArticleSummary(summary=f"{a.heading}: " + " ".join(
                                 merged.get(k.section_id, {}).get("summary", "") for k in kids[:3])[:400]),
                             prompt_version=a_version)
        return a.section_id, res.summary, child_hash, False
    with span("node", f"enrich-articles:{doc_id}", input={"articles": len(articles)}) as sp:
        art = _run_parallel(reduce_article, articles) if articles else []
        sp.output = {"articles": len(art), "reused": sum(1 for x in art if x[3])}

    # ---- reduce 2: document fields from article summaries + signals
    d_tmpl, _ = load("digitizer_doc")
    outline = [{"article": f"{by_id[sid].number or ''} {by_id[sid].heading}", "summary": summ} for sid, summ, _, _ in art] or \
              [{"section": f"{s.number or ''} {s.heading}", "summary": merged.get(s.section_id, {}).get("summary")} for s in sections[:40]]
    user = json.dumps({"document": {k: doc[k] for k in ("doc_id", "title", "doc_type", "doc_date")}, "articles": outline,
                       "signals": [x.model_dump() for x in signals]})

    def offline_doc() -> DocFields:
        eff = next((x for x in signals if x.kind == "effective_date"), None)
        lim = next((x for x in signals if x.kind == "period_limit"), None)
        covered = sorted({t.split(".")[0] for m in merged.values() for t in m["term_tags"]})
        return DocFields(doc_summary=f"{doc['title']} dated {doc['doc_date']} for {doc['trader_id']} covering "
                                     f"{', '.join(covered) or 'no taxonomy terms'}.",
                         effective_date_in_text=CitedValue(value=eff.value, section_id=eff.section_id, quote=eff.quote) if eff else None,
                         doc_scope_note=CitedValue(value=lim.value, section_id=lim.section_id, quote=lim.quote) if lim else None)
    limiter.acquire(est_tokens(user) + 400)
    fields = llm.structured(name=f"digitizer-doc:{doc_id}", system=d_tmpl, user=user, schema=DocFields, offline=offline_doc,
                            prompt_version=d_version)
    # guardrails: cited values must be verifiable; scope must be a period limit; effective date must be a date
    from ..tools.verify import verify_quote
    eff_date = fields.effective_date_in_text
    if eff_date and not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", eff_date.value or "")
                         and verify_quote(doc_id, eff_date.section_id, eff_date.quote).get("in_section")):
        eff_date = None
    scope = fields.doc_scope_note
    if scope and not (is_period_limit(scope.value) and verify_quote(doc_id, scope.section_id, scope.quote).get("in_section")):
        scope = None
    terms = sorted({t.split(".")[0] for m in merged.values() for t in m["term_tags"] if t.split(".")[0] in taxonomy()})

    # ---- switch: write everything for this document in one transaction
    now = datetime.now(timezone.utc).isoformat()
    with tx() as c:
        for sid, e in results.items():
            sig = [x.model_dump() for x in signals if x.section_id == sid]
            c.execute("INSERT OR REPLACE INTO section_enrichment(section_hash, prompt_version, model, summary, term_tags_json,"
                      " defined_terms_json, cross_refs_json, scope_notes_json, signals_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (hashes[sid], version, model, e.summary, j(e.term_tags), j(e.defined_terms), j(e.cross_refs), j(e.scope_notes),
                       j(sig), now))
        for sid, m in merged.items():
            c.execute("UPDATE sections SET summary=?, term_tags_json=?, defined_terms_json=?, cross_refs_json=?, scope_notes_json=?"
                      " WHERE section_id=?", (m["summary"], j(m["term_tags"]), j(m["defined_terms"]), j(m["cross_refs"]),
                                              j(m["scope_notes"]), sid))
        c.execute("DELETE FROM article_enrichment WHERE doc_id=?", (doc_id,))
        for sid, summ, child_hash, _ in art:
            c.execute("INSERT INTO article_enrichment(section_id, doc_id, child_hash, summary, prompt_version, model, created_at)"
                      " VALUES (?,?,?,?,?,?,?)", (sid, doc_id, child_hash, summ, a_version, model, now))
        c.execute("INSERT INTO doc_enrichment(doc_id, doc_summary, terms_covered_json, effective_date_in_text, doc_scope_note, model,"
                  " prompt_version, enriched_at, content_hash) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(doc_id) DO UPDATE SET"
                  " doc_summary=excluded.doc_summary, terms_covered_json=excluded.terms_covered_json,"
                  " effective_date_in_text=excluded.effective_date_in_text, doc_scope_note=excluded.doc_scope_note,"
                  " model=excluded.model, prompt_version=excluded.prompt_version, enriched_at=excluded.enriched_at,"
                  " content_hash=excluded.content_hash",
                  (doc_id, fields.doc_summary, j(terms), eff_date.value if eff_date else None, scope.value if scope else None,
                   model, full_version, now, content_hash))
    return {"cached": False, "sections": len(sections), "enriched": len(results), "reused": len(cache), "batches": len(batches),
            "articles": len(art), "retried_sections": retried, "dropped_unknown_tags": bad_tags, "signals": len(signals),
            "effective_date_in_text": eff_date.value if eff_date else None, "doc_scope_note": scope.value if scope else None}


def tags_for(section_ids: list[str]) -> dict[str, list[str]]:
    rows = q(f"SELECT section_id, term_tags_json FROM sections WHERE section_id IN ({','.join('?' * len(section_ids))})",
             tuple(section_ids)) if section_ids else []
    return {r["section_id"]: json.loads(r["term_tags_json"] or "[]") for r in rows}
