"""End-to-end offline: real Docling ingest of two traders' contracts -> graph -> review pause -> resume -> publish."""
import pytest

from compdoc import service
from compdoc.db import q
from compdoc.tools.access import AccessDenied
from compdoc.tools.clauses import find_docs_with_term
from compdoc.tools.ledger import normalize_value, record_finding

DOCS = ["D-005", "D-006", "D-007", "D-008", "D-009", "D-010", "D-011"]  # T-1003 (4 docs) + T-1004 (3 docs, same-date conflict)


@pytest.fixture(scope="module", autouse=True)
def ingested():
    service.ingest_all(doc_ids=DOCS)


def test_sections_built_with_numbering():
    secs = q("SELECT number, heading, parent_id FROM sections WHERE doc_id='D-005' ORDER BY ord")
    nums = [s["number"] for s in secs]
    assert "2.2" in nums and "3.3" in nums
    assert next(s for s in secs if s["number"] == "2.2")["heading"] == "Payout"
    assert next(s for s in secs if s["number"] == "2.2")["parent_id"] == "D-005:s2"


def test_extract_publishes_amended_values():
    res = service.extract_trader("T-1003")
    assert res["status"] == "published"
    ex = service.get_extract("T-1003")
    lf = ex.leaf("Compensation.Payout.payout_pct_net_pnl")
    assert lf.value == 17 and lf.source.doc_id == "D-007" and [h.value for h in lf.history] == [15]
    assert ex.leaf("Termination.Notice.notice_period_days").value == 90
    assert ex.leaf("Compensation.Base.base_salary").source.doc_id == "D-005"  # not mentioned later -> kept


def test_conflict_pauses_then_resume_publishes():
    res = service.extract_trader("T-1004")
    assert res["status"] == "pending_review"
    item = res["review"]["items"][0]
    assert item["kind"] == "same_date_different_value"
    pick = next(c for c in item["candidates"] if c["value"] == 18)
    out = service.resume_review(res["run_id"], {item["path"]: {"action": "pick", "finding_id": pick["finding_id"], "reason": "test"}}, "tester")
    assert out["status"] == "published"
    assert service.get_extract("T-1004").leaf(item["path"]).value == 18


def test_entitlements():
    with pytest.raises(AccessDenied):
        service.extract_trader("T-1004", caller="analyst_b")
    res = find_docs_with_term("Compensation.Payout", caller="analyst_b")
    assert res["count"] > 0 and all(r["summary"] is None for r in res["results"])


def test_record_finding_guards():
    r = record_finding("T-1003", "Compensation.Payout.payout_pct_net_pnl", None,
                       "The Payout provisions of the Agreement are deleted and replaced with the following", "D-007", "D-007:s2.1",
                       assertion="removed")
    assert not r["recorded"] and "REPLACES" in r["error"]
    r = record_finding("T-1003", "Compensation.Payout.payout_pct_net_pnl", "17%", "seventeen percent (17%) of Net Trading P&L",
                       "D-007", "D-007:s2.1", scope_note="for each Performance Year")
    assert r["recorded"] and r["value"] == 17 and r["warnings"]
    assert not record_finding("T-1003", "Bogus.Path.x", 1, "q", "D-007", "D-007:s2.1")["recorded"]
    assert normalize_value("US$1,500,000", "usd") == 1500000 and normalize_value("yes", None) is True
