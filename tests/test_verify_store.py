"""Part 4: quote verification and location from the text store, never from Docling JSON."""
import json
import pathlib

import pytest

from compdoc import service
from compdoc.db import q, q1
from compdoc.tools.verify import locate_quote, tolerant, verify_quote


@pytest.fixture(scope="module", autouse=True)
def ingested():
    service.ingest_all(doc_ids=["D-005", "D-006", "D-007", "D-008"])


@pytest.fixture
def no_docling_json(monkeypatch):
    """Fail the test if anything opens a cached Docling JSON file."""
    real = pathlib.Path.read_text

    def guard(self, *a, **kw):
        if str(self).endswith(".docling.json"):
            raise AssertionError(f"Docling JSON opened at run time: {self}")
        return real(self, *a, **kw)
    monkeypatch.setattr(pathlib.Path, "read_text", guard)


def test_verify_exact_and_value(no_docling_json):
    r = verify_quote("D-007", "D-007:s2.1", "seventeen percent (17%) of Net Trading P&L", 17)
    assert r["verified"] and r["match"] == "exact"
    r = verify_quote("D-007", "D-007:s2.1", "seventeen percent (17%) of Net Trading P&L", 18)
    assert not r["verified"] and r["reason"] == "value not in quote"


def test_verify_reports_other_section_without_json(no_docling_json):
    r = verify_quote("D-005", "D-005:s2.1", "The Deferral Threshold is US$1,500,000", 1500000)
    assert not r["verified"] and "D-005:s3.2" in r["found_in"]


def test_tolerant_match_for_ocr_hyphenation(no_docling_json):
    sec = q1("SELECT text FROM sections WHERE section_id='D-005:s2.2'")["text"]
    broken = sec.replace("discretionary", "discre-\ntionary")
    assert tolerant(broken).find(tolerant("discretionary payout")) >= 0


def test_locate_returns_item_page_box(no_docling_json):
    loc = locate_quote("D-005", "D-005:s2.2", "fifteen percent (15%) of Net Trading P&L")
    assert loc["docling_ref"].startswith("#/texts/") and loc["page"] == 1 and loc["bbox"]["l"] is not None


def test_spans_cover_section_text():
    for s in q("SELECT section_id, text FROM sections WHERE doc_id='D-005' AND text != ''"):
        spans = q('SELECT start, "end" FROM section_spans WHERE section_id=?', (s["section_id"],))
        assert spans and max(sp["end"] for sp in spans) == len(s["text"])


def test_cross_page_quote_gets_two_boxes(no_docling_json):
    row = q1("SELECT a.section_id FROM section_spans a JOIN section_spans b ON a.section_id=b.section_id "
             "AND a.item_ref=b.item_ref AND a.page < b.page LIMIT 1")
    if not row:
        pytest.skip("large set not ingested")
    sid = row["section_id"]
    text = q1("SELECT text FROM sections WHERE section_id=?", (sid,))["text"]
    quote = " ".join(text.split()[:30])
    loc = locate_quote(sid.split(":")[0], sid, quote)
    assert len({b["page"] for b in loc["boxes"]}) >= 2
