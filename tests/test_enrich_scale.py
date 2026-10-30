"""Part 2: enrichment map-reduce with a per-section cache (offline provider)."""
import pytest

from compdoc import service
from compdoc.agents.digitizer import _pack, enrich_document
from compdoc.db import q1
from compdoc.ingest import load_sections, store_sections


@pytest.fixture(scope="module", autouse=True)
def ingested():
    service.ingest_all(doc_ids=["D-005", "D-013", "D-019"])


def test_unchanged_document_is_skipped():
    assert enrich_document("D-005")["cached"] is True


def test_one_changed_section_reenriches_only_that_section():
    original = load_sections("D-005")
    secs = load_sections("D-005")
    target = next(s for s in secs if s.section_id == "D-005:s2.2")
    target.text = target.text.replace("fifteen percent (15%)", "sixteen percent (16%)")
    try:
        store_sections("D-005", secs)
        r = enrich_document("D-005")
        assert r["cached"] is False and r["enriched"] == 1 and r["reused"] == len(secs) - 1
    finally:
        store_sections("D-005", original)  # leave the shared test DB as it was
        enrich_document("D-005")


def test_doc_signals_cited_and_guarded():
    assert q1("SELECT effective_date_in_text FROM doc_enrichment WHERE doc_id='D-019'")["effective_date_in_text"] == "2025-01-01"
    assert "Performance Year 2025 only" in q1("SELECT doc_scope_note FROM doc_enrichment WHERE doc_id='D-013'")["doc_scope_note"]


def test_batches_packed_by_tokens():
    secs = load_sections("D-005")
    batches = _pack(secs, budget=200)
    assert len(batches) > 1 and sum(len(b) for b in batches) == len(secs)
    assert [s.section_id for b in batches for s in b] == [s.section_id for s in secs]  # order kept
