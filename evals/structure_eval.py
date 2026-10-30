"""Section-structure eval (scaling plan, Part 5 / milestone S1).

Compares the section tree built from each document's cached Docling output with the gold tree written by
data/synth/generate_large.py (structure/<doc_id>.json), for the new builder and the legacy builder.

Metrics per document:
  section P/R/F1   built section ids vs gold ids (an id encodes the normalised label path, e.g. D:s7.2.a.i)
  parent acc       for sections found in both, is the parent the same?
  missing / extra  examples of gold sections not built, and built sections not in gold
Usage: uv run python evals/structure_eval.py [--source data/synth/out_large] [--legacy]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def score(gold: list[dict], built: list) -> dict:
    g = {x["id"]: x for x in gold}
    b = {s.section_id: s for s in built if s.level > 0}
    inter = set(g) & set(b)
    p = len(inter) / len(b) if b else 0.0
    r = len(inter) / len(g) if g else 1.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    par_ok = sum(1 for i in inter if (b[i].parent_id or None) == (g[i]["parent"] or None))
    return {"gold": len(g), "built": len(b), "precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3),
            "parent_acc": round(par_ok / len(inter), 3) if inter else 0.0,
            "missing": sorted(set(g) - set(b))[:6], "extra": sorted(set(b) - set(g))[:6]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(ROOT / "data/synth/out_large"))
    args = ap.parse_args()
    from docling_core.types.doc import DoclingDocument

    from compdoc.db import q1
    from compdoc.ingest import build_sections, build_sections_legacy

    rows = []
    for gp in sorted((Path(args.source) / "structure").glob("*.json")):
        doc_id = gp.stem
        r = q1("SELECT c.docling_json_path FROM documents d JOIN conversions c ON c.file_hash=d.file_hash WHERE d.doc_id=?", (doc_id,))
        if not r:
            print(f"{doc_id}: not ingested - run `compdoc ingest --source {args.source}`")
            continue
        doc = DoclingDocument.load_from_json(Path(r["docling_json_path"]))
        gold = json.loads(gp.read_text())
        new, stats = build_sections(doc_id, doc)
        old, _ = build_sections_legacy(doc_id, doc)
        rows.append({"doc": doc_id, "new": score(gold, new), "legacy": score(gold, old),
                     "styles": stats.get("label_styles"), "toc_pages": stats.get("toc_pages"),
                     "sequence_issues": stats.get("sequence_issues"), "dropped": stats.get("dropped_items")})
    print(f"{'doc':8} {'gold':>5} | {'legacy F1':>9} {'parent':>6} | {'new F1':>6} {'parent':>6} {'built':>5} | toc  seq  dropped")
    for x in rows:
        n, o = x["new"], x["legacy"]
        print(f"{x['doc']:8} {n['gold']:>5} | {o['f1']:>9} {o['parent_acc']:>6} | {n['f1']:>6} {n['parent_acc']:>6} {n['built']:>5} | "
              f"{str(x['toc_pages']):4} {x['sequence_issues']:>3} {x['dropped']:>8}")
    for x in rows:
        if x["new"]["missing"] or x["new"]["extra"]:
            print(f"  {x['doc']} missing {x['new']['missing']}\n  {' ' * len(x['doc'])} extra   {x['new']['extra']}")
    if rows:
        avg = lambda k, which: round(sum(x[which][k] for x in rows) / len(rows), 3)  # noqa: E731
        print(f"\nMEAN legacy F1 {avg('f1', 'legacy')} parent {avg('parent_acc', 'legacy')}  |  new F1 {avg('f1', 'new')} "
              f"parent {avg('parent_acc', 'new')}")
        out = ROOT / "evals" / "results" / "structure-latest.json"
        out.write_text(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main()
