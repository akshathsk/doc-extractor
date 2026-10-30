import pytest

from compdoc.db import tx
from compdoc.schemas import Citation, Finding
from compdoc.tools.reconcile import reconcile_findings

T = "T-9001"


@pytest.fixture(autouse=True)
def docs():
    with tx() as c:
        c.execute("DELETE FROM documents WHERE trader_id=?", (T,))
        c.execute("DELETE FROM doc_enrichment WHERE doc_id LIKE 'R-%'")
        for did, date in [("R-1", "2023-01-01"), ("R-2", "2024-01-01"), ("R-3", "2024-01-01"), ("R-4", "2025-01-01")]:
            c.execute("INSERT INTO documents(doc_id, trader_id, doc_date, title, doc_type) VALUES (?,?,?,?,?)", (did, T, date, did, "x"))


_id = iter(range(1, 10_000))


def f(path, value, doc, date, assertion="set", scope=None, eff=None, verified=True):
    return Finding(finding_id=next(_id), trader_id=T, path=path, value=value, assertion=assertion, scope_note=scope,
                   source=Citation(doc_id=doc, section_id=f"{doc}:s1", doc_date=date, effective_date=eff, quote="q"),
                   quote_verified=verified)


P = "Compensation.Payout.payout_pct_net_pnl"
S = "Compensation.Base.base_salary"


def test_latest_wins_and_history():
    ex = reconcile_findings(T, [f(P, 15, "R-1", "2023-01-01"), f(P, 17, "R-4", "2025-01-01")])
    lf = ex.leaf(P)
    assert lf.value == 17 and lf.status == "current" and [h.value for h in lf.history] == [15]


def test_omission_keeps_prior_value():
    ex = reconcile_findings(T, [f(S, 200000, "R-1", "2023-01-01"), f(P, 17, "R-4", "2025-01-01")])
    assert ex.leaf(S).value == 200000 and ex.leaf(S).source.doc_id == "R-1"


def test_same_date_conflict_flagged():
    ex = reconcile_findings(T, [f(P, 15, "R-1", "2023-01-01"), f(P, 18, "R-2", "2024-01-01"), f(P, 19, "R-3", "2024-01-01")])
    assert ex.leaf(P).status == "conflict"
    assert [c.kind for c in ex.conflicts] == ["same_date_different_value"]


def test_scoped_override_keeps_prior_provisionally():
    ex = reconcile_findings(T, [f(P, 15, "R-1", "2023-01-01"), f(P, 20, "R-4", "2025-01-01", scope="Performance Year 2025 only")])
    assert ex.leaf(P).value == 15 and ex.leaf(P).status == "conflict"
    assert ex.conflicts[0].kind == "scoped_override"


def test_removal():
    ex = reconcile_findings(T, [f(P, 15, "R-1", "2023-01-01"), f(P, None, "R-4", "2025-01-01", assertion="removed")])
    assert ex.leaf(P).status == "removed" and ex.leaf(P).value is None


def test_text_effective_date_beats_db_date():
    # R-4 signed 2025 but effective 2023-06-01 -> R-2 (2024) is later and wins
    ex = reconcile_findings(T, [f(S, 350000, "R-4", "2025-01-01", eff="2023-06-01"), f(S, 325000, "R-2", "2024-01-01")])
    assert ex.leaf(S).value == 325000


def test_resolution_pick_overrides():
    a, b = f(P, 18, "R-2", "2024-01-01"), f(P, 19, "R-3", "2024-01-01")
    ex = reconcile_findings(T, [a, b], {P: {"action": "pick", "finding_id": a.finding_id, "reason": "side letter superseded", "by": "t"}})
    assert ex.leaf(P).value == 18 and ex.leaf(P).status == "current"
    assert ex.conflicts[0].resolution["action"] == "pick"


def test_unverified_quote_needs_review():
    ex = reconcile_findings(T, [f(P, 17, "R-4", "2025-01-01", verified=False)])
    assert ex.leaf(P).status == "needs_review"
