"""Docling benchmark (scaling plan, milestone M1).

Measures, on THIS machine, for the large synthetic contracts:
  - cold start (model load) and seconds per page by page type: digital text, table (schedules), scanned (OCR)
  - whole-file conversion of a ~200-page agreement, with OCR on vs off
  - peak memory per conversion process
Each case runs in a fresh subprocess so memory and cold start are isolated.

Usage: uv run python benchmarks/docling_bench.py [--doc data/synth/out_large/docs/L1-001.pdf] [--quick]
Results: benchmarks/results/<machine>-<timestamp>.json (+ printed table)
"""
from __future__ import annotations

import argparse
import json
import platform
import resource
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def make_converter(ocr: bool, table_mode: str = "accurate"):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import OcrMacOptions, PdfPipelineOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption
    o = PdfPipelineOptions(do_ocr=ocr, do_table_structure=True)
    o.table_structure_options.mode = TableFormerMode.ACCURATE if table_mode == "accurate" else TableFormerMode.FAST
    o.ocr_options = OcrMacOptions()
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=o)})


def run_case(path: str, start: int, end: int, ocr: bool, table_mode: str) -> dict:
    """Runs inside a child process."""
    t0 = time.perf_counter()
    conv = make_converter(ocr, table_mode)
    conv.initialize_pipeline(__import__("docling.datamodel.base_models", fromlist=["InputFormat"]).InputFormat.PDF)
    t_init = time.perf_counter() - t0
    t1 = time.perf_counter()
    res = conv.convert(path, page_range=(start, end))
    secs = time.perf_counter() - t1
    pages = end - start + 1
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / 1024 / 1024 if sys.platform == "darwin" else rss / 1024
    return {"pages": pages, "init_s": round(t_init, 2), "convert_s": round(secs, 2), "s_per_page": round(secs / pages, 3),
            "status": str(res.status.value if hasattr(res.status, "value") else res.status), "texts": len(res.document.texts),
            "tables": len(res.document.tables), "peak_rss_mb": round(rss_mb)}


def find_pages(path: str) -> dict:
    """Pick page ranges by type using pypdfium2 (cheap probe): digital body, schedule tables, scanned."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(path)
    n = len(pdf)
    texts = [pdf[i].get_textpage().get_text_range() for i in range(n)]
    pdf.close()
    scanned = [i + 1 for i, t in enumerate(texts) if len(t.strip()) < 20]
    table = [i + 1 for i, t in enumerate(texts) if "Schedule A - Payout Grid" in t or "Schedule B - Vesting" in t]
    body = [i + 1 for i, t in enumerate(texts) if "Section" in t and i + 1 not in table and len(t) > 1500]
    return {"n": n, "scanned": scanned, "table": table, "body": body[10:20]}


def child_main(args) -> None:
    out = run_case(args.path, args.start, args.end, args.ocr, args.table_mode)
    print("RESULT " + json.dumps(out))


def spawn(path: str, start: int, end: int, ocr: bool, table_mode: str = "accurate") -> dict:
    cmd = [sys.executable, __file__, "--child", "--path", path, "--start", str(start), "--end", str(end),
           "--table-mode", table_mode] + (["--ocr"] if ocr else [])
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
    if not line:
        return {"error": (p.stderr or p.stdout)[-800:]}
    return json.loads(line[7:])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default=str(ROOT / "data/synth/out_large/docs/L1-001.pdf"))
    ap.add_argument("--quick", action="store_true", help="skip the whole-file runs")
    ap.add_argument("--child", action="store_true")
    ap.add_argument("--path")
    ap.add_argument("--start", type=int)
    ap.add_argument("--end", type=int)
    ap.add_argument("--ocr", action="store_true")
    ap.add_argument("--table-mode", default="accurate")
    args = ap.parse_args()
    if args.child:
        return child_main(args)

    info = find_pages(args.doc)
    print(f"{Path(args.doc).name}: {info['n']} pages · scanned {info['scanned']} · table pages {info['table']}")
    cases = []
    b0, b1 = info["body"][0], info["body"][-1]
    cases.append(("digital body, OCR off", args.doc, b0, b1, False, "accurate"))
    cases.append(("digital body, OCR on", args.doc, b0, b1, True, "accurate"))
    if info["table"]:
        cases.append(("table pages, accurate", args.doc, min(info["table"]), max(info["table"]), False, "accurate"))
        cases.append(("table pages, fast", args.doc, min(info["table"]), max(info["table"]), False, "fast"))
    if info["scanned"]:
        s = info["scanned"][0]
        cases.append(("scanned page, OCR on", args.doc, s, s, True, "accurate"))
    scan_doc = next((p for p in sorted((ROOT / "data/synth/out_large/docs").glob("*.pdf")) if find_pages(str(p))["scanned"] and
                     len(find_pages(str(p))["scanned"]) == find_pages(str(p))["n"]), None)
    if scan_doc:
        n = find_pages(str(scan_doc))["n"]
        cases.append((f"fully scanned doc ({scan_doc.name}), OCR on", str(scan_doc), 1, n, True, "accurate"))
    if not args.quick:
        cases.append(("WHOLE FILE, OCR off", args.doc, 1, info["n"], False, "accurate"))
        cases.append(("WHOLE FILE, OCR on (today's settings)", args.doc, 1, info["n"], True, "accurate"))

    rows = []
    for name, path, s, e, ocr, tm in cases:
        print(f"… {name} (pages {s}-{e})", flush=True)
        r = spawn(path, s, e, ocr, tm)
        rows.append({"case": name, "doc": Path(path).name, "range": f"{s}-{e}", **r})
        print("   ", json.dumps(r), flush=True)
    res = {"machine": f"{platform.machine()} {platform.processor()} {platform.platform()}", "python": platform.python_version(),
           "at": datetime.now().isoformat(timespec="seconds"), "doc": Path(args.doc).name, "pages": info["n"], "cases": rows}
    out = ROOT / "benchmarks" / "results" / f"{platform.machine()}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2))
    print(f"\n{'case':45} {'pages':>5} {'init s':>7} {'conv s':>7} {'s/page':>7} {'RSS MB':>7}")
    for r in rows:
        print(f"{r['case'][:45]:45} {r.get('pages', ''):>5} {r.get('init_s', ''):>7} {r.get('convert_s', ''):>7} "
              f"{r.get('s_per_page', ''):>7} {r.get('peak_rss_mb', ''):>7} {r.get('error', '')[:60]}")
    print(f"saved {out}")


if __name__ == "__main__":
    main()
