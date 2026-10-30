"""Deterministic reconciliation of the evidence ledger into a term tree.

Rules (see plan):
- The finding with the latest effective date wins; older ones become history.
  effective date = finding.effective_date -> document's stated effective date (enrichment) -> DB doc_date.
- A document that does not mention a leaf leaves the prior value in place (we only see findings that exist).
- assertion="removed" from the latest document marks the leaf removed.
- Conflicts are flagged, never silently resolved:
    same_date_different_value  two different values with the same latest effective date
    scoped_override            the latest value is limited in scope (e.g. one performance year) -> prior value stays provisional
    missing_date               no usable date
    unverified_quote           the winning value's quote could not be verified in the cited section
- `resolutions` (from the Reconciler agent or a human) override the provisional choice per path.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from ..db import q1
from ..schemas import Citation, Conflict, Finding, HistoryEntry, Leaf, SubTermNode, TermNode, TraderExtract, term_names


def _doc_scope(doc_id: str) -> str | None:
    r = q1("SELECT doc_scope_note FROM doc_enrichment WHERE doc_id=?", (doc_id,))
    return r["doc_scope_note"] if r else None


def _eff(f: Finding) -> str | None:
    if f.source.effective_date:
        return f.source.effective_date
    r = q1("SELECT effective_date_in_text FROM doc_enrichment WHERE doc_id=?", (f.source.doc_id,))
    return (r and r["effective_date_in_text"]) or f.source.doc_date


def reconcile_findings(trader_id: str, findings: list[Finding], resolutions: dict[str, dict] | None = None,
                       as_of: str | None = None) -> TraderExtract:
    resolutions = resolutions or {}
    by_path: dict[str, list[Finding]] = {}
    for f in findings:
        f.source.effective_date = _eff(f)
        f.scope_note = f.scope_note or _doc_scope(f.source.doc_id)
        by_path.setdefault(f.path, []).append(f)

    leaves: dict[str, Leaf] = {}
    conflicts: list[Conflict] = []
    for path, fs in sorted(by_path.items()):
        # de-duplicate identical statements from the same document
        uniq: dict[tuple, Finding] = {}
        for f in fs:
            uniq.setdefault((f.source.doc_id, repr(f.value), f.assertion), f)
        fs = sorted(uniq.values(), key=lambda f: (f.source.effective_date or "", f.finding_id or 0))
        undated = [f for f in fs if not f.source.effective_date]
        dated = [f for f in fs if f.source.effective_date]

        winner: Finding | None = None
        status = "current"
        conflict: Conflict | None = None
        decision = ""
        if dated:
            latest_date = dated[-1].source.effective_date
            latest = [f for f in dated if f.source.effective_date == latest_date]
            distinct = {(repr(f.value), f.assertion) for f in latest}
            unscoped_prior = [f for f in dated if f.source.effective_date != latest_date and not f.scope_note]
            if len(distinct) > 1:
                conflict = Conflict(path=path, kind="same_date_different_value", candidates=latest,
                                    note=f"{len(latest)} documents dated {latest_date} state different values")
                winner, status = latest[-1], "conflict"
                decision = "provisional: same-date conflict, latest finding shown"
            elif latest[-1].scope_note:
                prior = unscoped_prior[-1] if unscoped_prior else None
                conflict = Conflict(path=path, kind="scoped_override", candidates=[x for x in (prior, latest[-1]) if x],
                                    note=f"latest value is limited in scope: {latest[-1].scope_note}")
                winner, status = (prior or latest[-1]), "conflict"
                decision = "provisional: kept prior general value; scoped override pending review" if prior else \
                           "provisional: only value is scoped"
            else:
                winner = latest[-1]
                decision = f"latest effective date {latest_date} ({winner.source.doc_id})"
        if undated:
            conflicts.append(Conflict(path=path, kind="missing_date", candidates=undated, note="finding has no usable date"))
            if winner is None:
                winner, status, decision = undated[-1], "needs_review", "only undated evidence"
        assert winner is not None
        if conflict is None and not winner.quote_verified:
            conflict = Conflict(path=path, kind="unverified_quote", candidates=[winner],
                                note="quote for the winning value could not be verified in the cited section")
            status = "needs_review"
            decision += "; quote unverified"
        if conflict:
            conflicts.append(conflict)

        res = resolutions.get(path)
        if res:
            if res.get("action") in ("pick", "keep_prior") and res.get("finding_id") is not None:
                chosen = next((f for f in fs if f.finding_id == res["finding_id"]), None)
                if chosen:
                    winner, status = chosen, "current"
            elif res.get("action") == "manual":
                winner = winner.model_copy(update={"value": res.get("value")})
                status = "current"
            if res.get("action") != "escalate":
                decision = f"resolved by {res.get('by', 'reviewer')}: {res.get('reason', '')}".strip()
                if conflict:
                    conflict.resolution = res

        if winner.assertion == "removed" and status == "current":
            status = "removed"
        history = [HistoryEntry(value=f.value, doc_id=f.source.doc_id, doc_date=f.source.doc_date,
                                effective_date=f.source.effective_date, assertion=f.assertion)
                   for f in fs if f is not winner]
        key = path.split(".")[-1]
        leaves[path] = Leaf(key=key, value=winner.value, unit=winner.unit, status=status,
                            source=Citation(**winner.source.model_dump()), history=history,
                            conflict=({"kind": conflict.kind, "note": conflict.note,
                                       "candidates": [c.finding_id for c in conflict.candidates],
                                       "resolution": conflict.resolution} if conflict else None),
                            decision=decision)

    order = {t: i for i, t in enumerate(term_names())}
    terms: dict[str, dict[str, list[Leaf]]] = {}
    for path, lf in leaves.items():
        t, s, _ = path.split(".", 2)
        terms.setdefault(t, {}).setdefault(s, []).append(lf)
    tree = [TermNode(term=t, sub_terms=[SubTermNode(sub_term=s, leaves=ls) for s, ls in subs.items()])
            for t, subs in sorted(terms.items(), key=lambda kv: order.get(kv[0], 99))]
    return TraderExtract(trader_id=trader_id, as_of=as_of or date.today().isoformat(), terms=tree, conflicts=conflicts)


def open_conflicts(extract: TraderExtract) -> list[Conflict]:
    return [c for c in extract.conflicts if not c.resolution or c.resolution.get("action") == "escalate"]


def conflict_payload(c: Conflict) -> dict[str, Any]:
    return {"path": c.path, "kind": c.kind, "note": c.note,
            "candidates": [{"finding_id": f.finding_id, "value": f.value, "assertion": f.assertion, "doc_id": f.source.doc_id,
                            "doc_date": f.source.doc_date, "effective_date": f.source.effective_date,
                            "section_id": f.source.section_id, "quote": f.source.quote, "scope_note": f.scope_note,
                            "quote_verified": f.quote_verified} for f in c.candidates]}
