"""Summarizer: short narrative of current terms, changes over time and open items."""
from __future__ import annotations

import json

from .. import llm
from ..prompts import load
from ..schemas import TraderExtract
from ..tools.reconcile import open_conflicts


def summarize(extract: TraderExtract) -> str:
    tmpl, version = load("summarizer")
    flat = extract.flat()
    compact = {p: {"value": lf.value, "unit": lf.unit, "status": lf.status, "source": f"{lf.source.doc_id} {lf.source.effective_date}",
                   "history": [f"{h.value} ({h.doc_id} {h.effective_date})" for h in lf.history]} for p, lf in flat.items()}
    open_items = [f"{c.kind}: {c.path}" for c in open_conflicts(extract)]

    def offline() -> str:
        parts = []
        for p, lf in flat.items():
            if lf.history:
                parts.append(f"{p.split('.')[-1]} is {lf.value} per {lf.source.doc_id} ({lf.source.effective_date}), "
                             f"previously {', '.join(str(h.value) for h in lf.history)}")
        head = f"{extract.trader_id}: {len(flat)} terms extracted."
        changed = (" Changes: " + "; ".join(parts) + ".") if parts else " No changes since the base agreement."
        opens = (" Open items: " + "; ".join(open_items) + ".") if open_items else ""
        return head + changed + opens

    return llm.complete(name=f"summarizer:{extract.trader_id}", system=tmpl, prompt_version=version, offline=offline,
                        user=json.dumps({"trader_id": extract.trader_id, "terms": compact, "open_items": open_items}, default=str))
