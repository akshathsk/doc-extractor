"""Rule-based extraction used by offline policies (no LLM). Tuned to the synthetic contracts' wording;
it doubles as a regression baseline for evals. Live mode uses the LLM agents instead."""
from __future__ import annotations

import re

from ..schemas import known_leaves

M = r"(?:US)?\$([\d,]+)"
W = r"[a-z-]+(?: [a-z-]+)?"
PATTERNS: dict[str, list[tuple[str, object]]] = {
    "base_salary": [(rf"base salary[^.]*?{M}", "num")],
    "payout_pct_net_pnl": [(rf"payout (?:equal to|shall be) {W} percent \((\d+)%\)", "num")],
    "hurdle_amount": [(rf"hurdle of {M}", "num")],
    "hwm_applies": [(r"no high-water mark|high-water mark provisions do not apply", False),
                    (r"subject to a high-water mark|a high-water mark applies", True)],
    "netting_scope": [(r"netted at the ([a-z ]+?) level", "text"), (r"netting shall be applied at the ([a-z ]+?) level", "text")],
    "deferral_pct": [(rf"percent \((\d+)%\) of (?:any )?payout (?:in excess of|above)", "num")],
    "deferral_threshold": [(rf"Deferral Threshold is {M}", "num"), (rf"payout amounts above {M}", "num")],
    "vesting_years": [(rf"vests? over (?:a period of )?{W} \((\d+)\) years", "num")],
    "vesting_type": [(r"on a ([a-z-]+) basis", "text")],
    "clawback_lookback_years": [(rf"clawback by the Firm for a period of {W} \((\d+)\) years", "num"),
                                (rf"recover payouts made within the preceding {W} \((\d+)\) years", "num")],
    "guaranteed_min_payout": [(rf"minimum payout of {M}", "num"), (rf"no less than {M}", "num")],
    "draw_amount": [(rf"draw of {M}", "num")],
    "noncompete_months": [(r"\((\d+)\) months following termination the Trader shall not engage", "num"),
                          (rf"Competing Business for a period of {W} \((\d+)\) months", "num")],
    "nonsolicit_months": [(r"\((\d+)\) months following termination the Trader shall not solicit", "num"),
                          (rf"not solicit[^.]*for {W} \((\d+)\) months", "num")],
    "notice_period_days": [(r"\((\d+)\) days' written notice", "num")],
    "garden_leave_days": [(rf"garden leave for up to {W} \((\d+)\) days", "num"), (rf"up to {W} \((\d+)\) days of garden leave", "num")],
}
HEADING_TO_LEAF = {
    "base salary": "base_salary", "payout": "payout_pct_net_pnl", "hurdle": "hurdle_amount", "high-water mark": "hwm_applies",
    "netting": "netting_scope", "deferral": "deferral_pct", "deferral threshold": "deferral_threshold",
    "vesting": "vesting_years", "vesting method": "vesting_type", "clawback": "clawback_lookback_years",
    "guaranteed minimum payout": "guaranteed_min_payout", "draw": "draw_amount", "non-competition": "noncompete_months",
    "non-solicitation": "nonsolicit_months", "notice period": "notice_period_days", "garden leave": "garden_leave_days",
}


def _sentences(text: str) -> list[str]:
    text = text.replace("\n", " ")
    return [s.strip() for s in re.split(r"(?<=[.])\s+(?=[A-Z\"])", text) if s.strip()]


def extract_values(text: str, heading: str, term: str) -> list[dict]:
    """Return [{leaf, path, value, quote, assertion}] for leaves of `term` found in `text`."""
    leaves = {k: v for k, v in known_leaves().items() if v["path"].startswith(term + ".")}
    out: list[dict] = []
    if re.search(r"deleted in (?:their|its) entirety", text, re.I):
        leaf = HEADING_TO_LEAF.get(heading.strip().lower())
        if leaf in leaves:
            q = next((s for s in _sentences(text) if "deleted in" in s.lower()), text[:200])
            out.append({"leaf": leaf, "path": leaves[leaf]["path"], "value": None, "quote": q, "assertion": "removed"})
        return out
    for sent in _sentences(text):
        for leaf, pats in PATTERNS.items():
            if leaf not in leaves:
                continue
            for pat, kind in pats:
                m = re.search(pat, sent, re.I)
                if not m:
                    continue
                if kind == "num":
                    val = int(m.group(1).replace(",", ""))
                elif kind == "text":
                    val = m.group(1).strip()
                else:
                    val = kind
                out.append({"leaf": leaf, "path": leaves[leaf]["path"], "value": val, "quote": sent, "assertion": "set"})
                break
    seen, uniq = set(), []
    for o in out:
        if (o["path"], o["value"]) not in seen:
            seen.add((o["path"], o["value"]))
            uniq.append(o)
    return uniq
