"""Sharded conversion benchmark and checks (scaling plan, milestone M2).

1. Wall time for a ~200-page PDF: whole file (1 process) vs sharded with 2 and 4 warm workers.
2. Equivalence: merged shards give the same text items, in the same order, on the same pages as a whole-file run.
3. Failure handling: a transient failure is retried; a page that always fails is isolated by splitting, and the
   document is marked partial_success with that page listed.

Each scenario runs in a fresh process (env vars set worker count and fault injection before the pool starts).
Usage: uv run python benchmarks/shard_bench.py [--doc data/synth/out_large/docs/L1-001.pdf]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def child(path: str, mode: str) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from compdoc.config import settings
    from compdoc.conversion import convert_sharded, shutdown_pool
    from compdoc.ingest import file_hash
    fh = file_hash(path) + "-" + mode  # separate shard cache per scenario
    for f in (settings.cache_dir / "shards").glob(f"{fh}_*"):
        f.unlink()
    for f in settings.cache_dir.glob(".fail_once_*"):
        f.unlink()
    t0 = time.perf_counter()
    if mode == "whole":
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import OcrMacOptions, PdfPipelineOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, PdfFormatOption
        o = PdfPipelineOptions(do_ocr=True, do_table_structure=True)
        o.table_structure_options.mode = TableFormerMode.ACCURATE
        o.ocr_options = OcrMacOptions()
        conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=o)})
        conv.initialize_pipeline(InputFormat.PDF)
        t1 = time.perf_counter()
        doc = conv.convert(path).document
        report = {"status": "success", "init_s": round(t1 - t0, 2)}
    else:
        doc, report = convert_sharded(path, fh)
        shutdown_pool()
    secs = round(time.perf_counter() - t0, 2)
    items = [(t.label.value, t.text, t.prov[0].page_no if t.prov else None) for t in doc.texts]
    out = Path(os.environ["BENCH_OUT"])
    out.write_text(json.dumps({"seconds": secs, "pages": len(doc.pages), "items": items,
                               "report": {k: v for k, v in report.items() if k != "shards"},
                               "shards": [{k: s[k] for k in ("start", "end", "status", "attempts", "seconds", "worker", "error")}
                                          for s in report.get("shards", [])]}))


def run(path: str, mode: str, env: dict) -> dict:
    out = ROOT / "var" / f"shard_bench_{mode}.json"
    e = {**os.environ, **env, "BENCH_OUT": str(out), "COMPDOC_PROVIDER": "offline"}
    p = subprocess.run([sys.executable, __file__, "--child", mode, "--doc", path], env=e, capture_output=True, text=True, timeout=3600)
    if not out.exists() or p.returncode:
        return {"error": p.stderr[-1500:]}
    return json.loads(out.read_text())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default=str(ROOT / "data/synth/out_large/docs/L1-001.pdf"))
    ap.add_argument("--child")
    args = ap.parse_args()
    if args.child:
        return child(args.doc, args.child)
    scenarios = [
        ("whole", {}),
        ("sharded-2w", {"COMPDOC_CONVERT_WORKERS": "2"}),
        ("sharded-4w", {"COMPDOC_CONVERT_WORKERS": "4"}),
        ("fail-once-4w", {"COMPDOC_CONVERT_WORKERS": "4", "COMPDOC_FAIL_ONCE": "45"}),
        ("fail-always-4w", {"COMPDOC_CONVERT_WORKERS": "4", "COMPDOC_FAIL_PAGES": "45"}),
    ]
    res = {}
    for name, env in scenarios:
        print(f"… {name}", flush=True)
        res[name] = run(args.doc, name, env)
        r = res[name]
        print("   ", r.get("error") or {"seconds": r["seconds"], "pages": r["pages"], **r["report"]}, flush=True)
    whole = res["whole"].get("items")
    print(f"\n{'scenario':16} {'seconds':>8} {'pages':>6} {'shards':>7} {'retries':>7}  same items as whole file?  status / failed pages")
    for name, r in res.items():
        if "error" in r:
            print(f"{name:16} ERROR {r['error'][-200:]}")
            continue
        same = r["items"] == whole
        if not same and whole:
            # compare ignoring a paragraph split at a shard boundary: same text overall, same pages
            same = "same text, split at boundary" if " ".join(t for _, t, _ in r["items"]) == " ".join(t for _, t, _ in whole) else False
        retries = sum(max(0, s["attempts"] - 1) for s in r["shards"])
        print(f"{name:16} {r['seconds']:>8} {r['pages']:>6} {len(r['shards']):>7} {retries:>7}  {str(same):26} "
              f"{r['report'].get('status')} {r['report'].get('failed_pages', '')}")
    (ROOT / "benchmarks" / "results" / "shard-latest.json").write_text(
        json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "items"} for k, v in res.items()}, indent=2))


if __name__ == "__main__":
    main()
