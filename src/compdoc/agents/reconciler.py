"""Reconciler: deterministic `reconcile` first; the LLM only looks at conflicts and proposes pick/keep_prior/escalate."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel

from .. import llm
from ..llm import ToolBox
from ..observability import span
from ..prompts import load
from ..schemas import TraderExtract
from ..tools.ledger import get_findings
from ..tools.reconcile import conflict_payload, open_conflicts, reconcile_findings
from ..tools.registry import reconciler_tools


class Decision(BaseModel):
    path: str
    action: Literal["pick", "keep_prior", "escalate"]
    finding_id: int | None = None
    reason: str


class Decisions(BaseModel):
    decisions: list[Decision]


def reconcile_run(trader_id: str, resolutions: dict[str, dict] | None = None) -> TraderExtract:
    with span("tool", "reconcile", input={"trader_id": trader_id, "resolutions": resolutions}) as s:
        ex = reconcile_findings(trader_id, get_findings(trader_id), resolutions)
        s.output = {"leaves": len(ex.flat()), "conflicts": [c.kind + ":" + c.path for c in ex.conflicts]}
    return ex


def propose_resolutions(trader_id: str, extract: TraderExtract) -> dict[str, dict]:
    """LLM looks only at open conflicts (with the cited sections re-read) and proposes resolutions."""
    conflicts = open_conflicts(extract)
    if not conflicts:
        return {}
    tmpl, version = load("reconciler")
    box = ToolBox(reconciler_tools(trader_id))
    payload = []
    for c in conflicts:
        p = conflict_payload(c)
        for cand in p["candidates"]:
            sec = box.call("read_section", doc_id=cand["doc_id"], section_id=cand["section_id"])
            cand["section_text"] = sec.get("text", "")[:1500]
            cand["doc_preamble_scope"] = sec.get("scope_notes")
        payload.append(p)

    def offline() -> Decisions:
        # Conservative: never auto-resolve. Same-date conflicts and scoped overrides go to a human.
        return Decisions(decisions=[Decision(path=c.path, action="escalate", reason=f"offline policy escalates {c.kind}")
                                    for c in conflicts])

    out = llm.structured(name=f"reconciler:{trader_id}", system=tmpl, user=json.dumps(payload, default=str), schema=Decisions,
                         offline=offline, prompt_version=version, effort="medium")
    valid = {c.path: {f.finding_id for f in c.candidates} for c in conflicts}
    res = {}
    for d in out.decisions:
        if d.path not in valid:
            continue
        if d.action != "escalate" and d.finding_id not in valid[d.path]:
            continue  # guardrail: must pick one of the candidates
        res[d.path] = {"action": d.action, "finding_id": d.finding_id, "reason": d.reason, "by": "reconciler-agent"}
    return res
