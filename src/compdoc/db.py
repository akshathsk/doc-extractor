"""SQLite storage. One file (var/compdoc.db) holds:

- documents        the mock of the firm's document DB: trader_id -> docs with dates (+ file hash)
- conversions      Docling conversion status + ingest QC per file hash
- sections         agent-facing sections built from Docling output (+ enrichment columns)
- doc_enrichment   document-level LLM enrichment
- findings         evidence ledger written by Term Researchers
- extracts         versioned, published TraderExtract JSON
- reviews          human review queue
- audit            append-only audit log
- entitlements     caller -> trader access (mock)
- drafts           drafted documents + precedent refs
- runs / spans     tracing (see observability.py)
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY, trader_id TEXT NOT NULL, trader_name TEXT, title TEXT, doc_type TEXT,
  doc_date TEXT, path TEXT, format TEXT, file_hash TEXT, ingested_at TEXT);
CREATE INDEX IF NOT EXISTS ix_documents_trader ON documents(trader_id);

CREATE TABLE IF NOT EXISTS conversions (
  file_hash TEXT PRIMARY KEY, status TEXT, qc_json TEXT, docling_json_path TEXT, markdown_path TEXT,
  converted_at TEXT, seconds REAL);

CREATE TABLE IF NOT EXISTS sections (
  section_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, ord INTEGER, number TEXT, heading TEXT, path_json TEXT,
  level INTEGER, parent_id TEXT, pages_json TEXT, text TEXT, tables_json TEXT, item_refs_json TEXT, bboxes_json TEXT,
  summary TEXT, term_tags_json TEXT, defined_terms_json TEXT, cross_refs_json TEXT, scope_notes_json TEXT);
CREATE INDEX IF NOT EXISTS ix_sections_doc ON sections(doc_id);

CREATE TABLE IF NOT EXISTS section_text (
  section_id TEXT PRIMARY KEY, doc_id TEXT, section_hash TEXT, norm_text TEXT);
CREATE INDEX IF NOT EXISTS ix_section_text_doc ON section_text(doc_id);
CREATE TABLE IF NOT EXISTS section_spans (
  section_id TEXT, start INTEGER, "end" INTEGER, item_ref TEXT, page INTEGER, l REAL, t REAL, r REAL, b REAL, origin TEXT);
CREATE INDEX IF NOT EXISTS ix_section_spans ON section_spans(section_id);

CREATE TABLE IF NOT EXISTS section_enrichment (
  section_hash TEXT, prompt_version TEXT, model TEXT, summary TEXT, term_tags_json TEXT, defined_terms_json TEXT,
  cross_refs_json TEXT, scope_notes_json TEXT, signals_json TEXT, created_at TEXT,
  PRIMARY KEY (section_hash, prompt_version, model));
CREATE TABLE IF NOT EXISTS article_enrichment (
  section_id TEXT PRIMARY KEY, doc_id TEXT, child_hash TEXT, summary TEXT, prompt_version TEXT, model TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_article_enrichment_doc ON article_enrichment(doc_id);

CREATE TABLE IF NOT EXISTS doc_enrichment (
  doc_id TEXT PRIMARY KEY, doc_summary TEXT, terms_covered_json TEXT, effective_date_in_text TEXT,
  doc_scope_note TEXT, model TEXT, prompt_version TEXT, enriched_at TEXT);

CREATE TABLE IF NOT EXISTS findings (
  finding_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, trader_id TEXT, path TEXT, value_json TEXT, unit TEXT,
  assertion TEXT, scope_note TEXT, source_json TEXT, quote_verified INTEGER, agent TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_findings_run ON findings(run_id, trader_id);

CREATE TABLE IF NOT EXISTS extracts (
  trader_id TEXT, version INTEGER, run_id TEXT, as_of TEXT, extract_json TEXT, created_at TEXT,
  PRIMARY KEY (trader_id, version));

CREATE TABLE IF NOT EXISTS reviews (
  review_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, thread_id TEXT, trader_id TEXT, kind TEXT,
  payload_json TEXT, status TEXT, decision_json TEXT, reviewer TEXT, created_at TEXT, decided_at TEXT);

CREATE TABLE IF NOT EXISTS audit (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, actor TEXT, action TEXT, trader_id TEXT, detail_json TEXT);

CREATE TABLE IF NOT EXISTS entitlements (caller TEXT, trader_id TEXT, PRIMARY KEY (caller, trader_id));

CREATE TABLE IF NOT EXISTS drafts (
  draft_id TEXT PRIMARY KEY, run_id TEXT, trader_id TEXT, request TEXT, path TEXT, draft_json TEXT,
  status TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, type TEXT, trader_id TEXT, caller TEXT, status TEXT, input_json TEXT, output_json TEXT,
  started_at TEXT, ended_at TEXT, latency_ms REAL, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
  cost REAL DEFAULT 0, error TEXT);

CREATE TABLE IF NOT EXISTS spans (
  span_id TEXT PRIMARY KEY, run_id TEXT, parent_id TEXT, kind TEXT, name TEXT, input_json TEXT, output_json TEXT,
  model TEXT, prompt_version TEXT, tokens_in INTEGER, tokens_out INTEGER, cost REAL, started_at TEXT, ended_at TEXT,
  latency_ms REAL, status TEXT, error TEXT, seq INTEGER);
CREATE INDEX IF NOT EXISTS ix_spans_run ON spans(run_id);
"""

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False


def connect() -> sqlite3.Connection:
    global _initialised
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        _local.conn = c
    if not _initialised:
        with _init_lock:
            if not _initialised:
                c.executescript(SCHEMA)
                for col in ("ALTER TABLE doc_enrichment ADD COLUMN content_hash TEXT",):  # light migrations
                    try:
                        c.execute(col)
                    except sqlite3.OperationalError:
                        pass
                c.commit()
                _initialised = True
    return c


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    c = connect()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def q(sql: str, params: tuple | dict = ()) -> list[dict]:
    return [dict(r) for r in connect().execute(sql, params).fetchall()]


def q1(sql: str, params: tuple | dict = ()) -> dict | None:
    r = connect().execute(sql, params).fetchone()
    return dict(r) if r else None


def j(v: Any) -> str:
    return json.dumps(v, default=str)


def uj(s: str | None, default: Any = None) -> Any:
    return json.loads(s) if s else default


def audit(actor: str, action: str, trader_id: str | None = None, **detail: Any) -> None:
    from datetime import datetime, timezone
    with tx() as c:
        c.execute("INSERT INTO audit(at, actor, action, trader_id, detail_json) VALUES (?,?,?,?,?)",
                  (datetime.now(timezone.utc).isoformat(), actor, action, trader_id, j(detail)))
