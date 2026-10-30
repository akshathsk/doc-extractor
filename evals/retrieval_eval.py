"""Retrieval recall eval (scaling plan, Part 3 / R4): do the candidate signals put the right section in the top k?

For every gold finding in the large synthetic set (some clauses paraphrased: "incentive compensation",
"Restricted Period", "recoup"...), the query is the finding's taxonomy sub-term, searched within its document:
  tags      sections tagged with the sub-term (an unordered set: recall = is the gold section tagged at all)
  bm25      keyword search on the sub-term's description and hints
  semantic  local embeddings, same query text
  hybrid    reciprocal rank fusion of the three
Note: offline, tags come from a keyword tagger; in live mode the LLM tags sections, so tag recall will be higher.
Usage: uv run python evals/retrieval_eval.py [--source data/synth/out_large] [--k 1 3 5]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

HEADINGS = {"base_salary": "Base Salary", "payout_pct_net_pnl": "Payout", "hurdle_amount": "Hurdle", "deferral_pct": "Deferral",
            "vesting_years": "Vesting", "clawback_lookback_years": "Clawback", "noncompete_months": "Non-Competition",
            "notice_period_days": "Notice Period"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(ROOT / "data/synth/out_large"))
    ap.add_argument("--k", type=int, nargs="+", default=[1, 3, 5])
    args = ap.parse_args()
    from compdoc.db import q, q1
    from compdoc.tools.hybrid import hybrid_search, semantic_search, term_query
    from compdoc.tools.search import search_sections

    rows = []
    for gp in sorted((Path(args.source) / "gold").glob("*.json")):
        gold = json.loads(gp.read_text())
        for f in gold["findings"]:
            leaf = f["path"].split(".")[-1]
            sub = ".".join(f["path"].split(".")[:2])
            g = q1("SELECT section_id, text FROM sections WHERE doc_id=? AND heading=? AND level > 0", (f["doc_id"], HEADINGS[leaf]))
            if not g:
                continue
            paraphrased = not any(w in g["text"] for w in ("payout equal to", "base salary of", "hurdle of", "Deferral Threshold shall",
                                                          "shall vest over", "subject to clawback", "Competing Business.",
                                                          "days' written notice"))
            qtext = term_query(sub)
            tagged = {r["section_id"] for r in q("SELECT section_id, term_tags_json FROM sections WHERE doc_id=?", (f["doc_id"],))
                      if any(t == sub or t.startswith(sub + ".") for t in json.loads(r["term_tags_json"] or "[]"))}
            bm = [h["section_id"] for h in search_sections(qtext, None, [f["doc_id"]], top_k=20)]
            se = [h["section_id"] for h in semantic_search(qtext, doc_ids=[f["doc_id"]], top_k=20)]
            hy = [h["section_id"] for h in hybrid_search(term_path=sub, doc_ids=[f["doc_id"]], top_k=20)]
            rank = lambda lst: lst.index(g["section_id"]) + 1 if g["section_id"] in lst else None  # noqa: E731
            rows.append({"doc": f["doc_id"], "path": f["path"], "paraphrased": paraphrased, "gold": g["section_id"],
                         "tagged": g["section_id"] in tagged, "n_tagged": len(tagged),
                         "bm25": rank(bm), "semantic": rank(se), "hybrid": rank(hy)})
    if not rows:
        print("no gold sections found - ingest the large set first")
        return

    def recall(sel, signal, k):
        sel = list(sel)
        return round(sum(1 for r in sel if r[signal] and r[signal] <= k) / len(sel), 3) if sel else None
    groups = {"all": rows, "plain wording": [r for r in rows if not r["paraphrased"]], "paraphrased": [r for r in rows if r["paraphrased"]]}
    print(f"{len(rows)} gold findings ({len(groups['paraphrased'])} paraphrased)\n")
    head = f"{'group':15} {'tags (any rank)':>16} " + " ".join(f"{s}@{k:<2}".rjust(12) for s in ("bm25", "semantic", "hybrid") for k in args.k)
    print(head)
    summary = {}
    for name, sel in groups.items():
        tags = round(sum(r["tagged"] for r in sel) / len(sel), 3) if sel else None
        vals = {f"{s}@{k}": recall(sel, s, k) for s in ("bm25", "semantic", "hybrid") for k in args.k}
        summary[name] = {"n": len(sel), "tags": tags, **vals}
        print(f"{name:15} {str(tags):>16} " + " ".join(str(vals[f'{s}@{k}']).rjust(12) for s in ("bm25", "semantic", "hybrid") for k in args.k))
    misses = [r for r in rows if not r["hybrid"] or r["hybrid"] > max(args.k)]
    if misses:
        print("\nhybrid misses:", [(r["doc"], r["path"].split(".")[-1], r["bm25"], r["semantic"]) for r in misses[:8]])
    (ROOT / "evals" / "results" / "retrieval-latest.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))


if __name__ == "__main__":
    main()
