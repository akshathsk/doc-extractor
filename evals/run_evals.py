"""Evaluation harness: run extraction for each trader and score it against data/synth/out/gold.

Metrics
  leaf P/R/F1          (path, value) of current/removed leaves vs gold current values
  current_value_acc    gold paths (excluding expected-conflict paths) whose extracted value matches
  finding_recall       gold per-document findings present in the run's evidence ledger
  citation_validity    share of leaves whose quote was verified in the cited section
  conflict P/R         flagged conflict paths vs expected conflicts (same-date, scoped override)
  term_tag_recall      gold findings whose document has a section tagged with the leaf's sub-term (Term Finder)
  cost                 tool calls, LLM calls, tokens, $ and latency per trader (from traces)

Usage: uv run python evals/run_evals.py [--traders T-1003 T-1004] [--provider offline] [--out evals/results/x.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--traders", nargs="*")
    ap.add_argument("--provider")
    ap.add_argument("--out")
    args = ap.parse_args()
    if args.provider:
        os.environ["COMPDOC_PROVIDER"] = args.provider

    from compdoc.config import settings
    from compdoc.db import q, q1
    from compdoc.schemas import TraderExtract, known_leaves
    from compdoc.service import extract_trader

    gold_dir = settings.source_dir / "gold"
    traders = args.traders or sorted(p.stem for p in gold_dir.glob("*.json"))
    rows = []
    for tid in traders:
        gold = json.loads((gold_dir / f"{tid}.json").read_text())
        res = extract_trader(tid)
        ex = TraderExtract.model_validate(res["extract"])
        flat = ex.flat()
        expected_conf = set(gold["expected_conflicts"])
        gold_pairs = {(p, json.dumps(g["value"])) for p, g in gold["current"].items() if p not in expected_conf}
        known = {v["path"] for v in known_leaves().values()}
        got_pairs = {(p, json.dumps(lf.value)) for p, lf in flat.items() if p not in expected_conf and p in known}
        extra_leaves = sorted(p for p in flat if p not in known)
        tp = len(gold_pairs & got_pairs)
        prec = tp / len(got_pairs) if got_pairs else 0.0
        rec = tp / len(gold_pairs) if gold_pairs else 1.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        acc_paths = [p for p in gold["current"] if p not in expected_conf]
        correct = 0
        wrong = []
        for p in acc_paths:
            g, lf = gold["current"][p], flat.get(p)
            ok = lf is not None and ((g.get("removed") and lf.status == "removed") or (not g.get("removed") and lf.value == g["value"]))
            correct += ok
            if not ok:
                wrong.append({"path": p, "gold": g["value"], "got": lf.value if lf else None})
        ledger = {(f["source"]["doc_id"], f["path"], json.dumps(f["value"])) for f in (
            {"source": json.loads(r["source_json"]), "path": r["path"], "value": json.loads(r["value_json"])}
            for r in q("SELECT * FROM findings WHERE run_id=?", (res["run_id"],)))}
        gold_f = {(f["doc_id"], f["path"], json.dumps(f["value"])) for f in gold["findings"]}
        found_conf = {c.path for c in ex.conflicts if c.kind in ("same_date_different_value", "scoped_override")}
        conf_tp = len(found_conf & expected_conf)
        tag_hits = 0
        for f in gold["findings"]:
            sub = ".".join(f["path"].split(".")[:2])
            tag_hits += bool(q1("SELECT 1 FROM sections WHERE doc_id=? AND term_tags_json LIKE ?", (f["doc_id"], f'%"{sub}"%')))
        run = q1("SELECT * FROM runs WHERE run_id=?", (res["run_id"],))
        n_tools = q1("SELECT count(*) n FROM spans WHERE run_id=? AND kind='tool'", (res["run_id"],))["n"]
        n_llm = q1("SELECT count(*) n FROM spans WHERE run_id=? AND kind='llm'", (res["run_id"],))["n"]
        rows.append({
            "trader_id": tid, "run_id": res["run_id"], "status": res["status"],
            "leaf_precision": round(prec, 3), "leaf_recall": round(rec, 3), "leaf_f1": round(f1, 3),
            "current_value_acc": round(correct / len(acc_paths), 3) if acc_paths else 1.0,
            "finding_recall": round(len(gold_f & ledger) / len(gold_f), 3) if gold_f else 1.0,
            "citation_validity": round(sum(1 for lf in flat.values() if lf.status != "needs_review") / len(flat), 3) if flat else 0,
            "conflict_recall": round(conf_tp / len(expected_conf), 3) if expected_conf else None,
            "conflict_precision": round(conf_tp / len(found_conf), 3) if found_conf else None,
            "term_tag_recall": round(tag_hits / len(gold["findings"]), 3) if gold["findings"] else 1.0,
            "tool_calls": n_tools, "llm_calls": n_llm, "tokens_in": run["tokens_in"], "tokens_out": run["tokens_out"],
            "cost_usd": round(run["cost"] or 0, 4), "latency_s": round((run["latency_ms"] or 0) / 1000, 1),
            "extra_leaves": extra_leaves, "wrong": wrong,
        })
        r = rows[-1]
        print(f"{tid}: F1={r['leaf_f1']} acc={r['current_value_acc']} findings={r['finding_recall']} "
              f"conflicts R={r['conflict_recall']} P={r['conflict_precision']} tags={r['term_tag_recall']} "
              f"tools={n_tools} llm={n_llm} ${r['cost_usd']} {r['latency_s']}s {res['status']}"
              + (f"  WRONG: {wrong}" if wrong else ""))

    def mean(k: str) -> float | None:
        vals = [r[k] for r in rows if r[k] is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    summary = {k: mean(k) for k in ("leaf_precision", "leaf_recall", "leaf_f1", "current_value_acc", "finding_recall",
                                    "citation_validity", "conflict_recall", "conflict_precision", "term_tag_recall")}
    summary.update(total_cost_usd=round(sum(r["cost_usd"] for r in rows), 4), total_tool_calls=sum(r["tool_calls"] for r in rows),
                   provider=settings.provider, agent_model=settings.agent_model, fast_model=settings.fast_model,
                   at=datetime.now().isoformat(timespec="seconds"))
    print("\nSUMMARY", json.dumps(summary, indent=2))
    out = Path(args.out) if args.out else ROOT / "evals" / "results" / f"{settings.provider}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "traders": rows}, indent=2, default=str))
    print(f"saved {out}")


if __name__ == "__main__":
    main()
