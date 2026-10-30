"""Sharded Docling conversion (scaling plan, Part 1 / milestone M2).

coordinator (this process)                      warm workers (process pool, models loaded once)
  probe pages (pypdfium2: text layer?)            convert(path, page_range=(start, end))
  plan shards (~40 pages, split at scan runs)     -> DoclingDocument JSON for that range
  shard cache (file hash + range + pipeline)
  submit, retry x2, then split in half
  merge (DoclingDocument.concatenate, page order)
  coverage check: every page 1..N exactly once

Workers are code, not LLM agents. Small PDFs and DOCX are converted whole (no gain from sharding).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from importlib.metadata import version
from pathlib import Path

from docling_core.types.doc import DoclingDocument

from .config import settings

SHARD_PAGES = int(os.environ.get("COMPDOC_SHARD_PAGES", "40"))
SHARD_MIN_PAGES = int(os.environ.get("COMPDOC_SHARD_MIN_PAGES", "60"))
# Default 1 = no sharding. Measured (benchmarks/shard_bench.py): on ONE CPU machine Docling already uses every core, so
# 2-4 local workers were 1.3-2x SLOWER than one whole-file process. Sharding pays off with workers on several
# machines or GPUs (milestone M3) and for failure isolation; enable with COMPDOC_CONVERT_WORKERS>=2.
WORKERS = int(os.environ.get("COMPDOC_CONVERT_WORKERS", "1"))
MAX_RETRIES = 2


def pipeline_version() -> str:
    opts = {"ocr": settings.ocr_engine, "table": "accurate", "docling": version("docling")}
    return "pv-" + hashlib.sha1(json.dumps(opts, sort_keys=True).encode()).hexdigest()[:10]


# ------------------------------------------------------------------------------------------ probe + plan
def probe(path: str) -> list[dict]:
    """Per page: has a text layer? (cheap: pypdfium2, ~ms per page)."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(path)
    pages = []
    for i in range(len(pdf)):
        chars = len(pdf[i].get_textpage().get_text_range().strip())
        pages.append({"page": i + 1, "chars": chars, "scanned": chars < 20})
    pdf.close()
    return pages


@dataclass
class Shard:
    start: int
    end: int
    scanned: bool
    attempts: int = 0
    status: str = "pending"  # pending | done | cached | failed
    seconds: float | None = None
    worker: int | None = None
    error: str | None = None
    docling_status: str | None = None
    children: list = field(default_factory=list)

    @property
    def pages(self) -> int:
        return self.end - self.start + 1


def plan_shards(pages: list[dict], size: int = SHARD_PAGES) -> list[Shard]:
    """Contiguous runs of digital / scanned pages, each cut into shards of at most `size` pages."""
    shards: list[Shard] = []
    run_start, run_scan = 1, pages[0]["scanned"] if pages else False
    for p in pages[1:] + [None]:
        if p is None or p["scanned"] != run_scan:
            end = (p["page"] - 1) if p else pages[-1]["page"]
            for s in range(run_start, end + 1, size):
                shards.append(Shard(s, min(s + size - 1, end), run_scan))
            if p:
                run_start, run_scan = p["page"], p["scanned"]
    return shards


# ------------------------------------------------------------------------------------------ workers
_converter = None


def _init_worker(threads: int) -> None:
    """Runs once per worker process: load Docling models (the ~6-10 s warm start is paid once)."""
    global _converter
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import EasyOcrOptions, OcrMacOptions, PdfPipelineOptions, TableFormerMode, TesseractCliOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    opts = PdfPipelineOptions(do_ocr=True, do_table_structure=True, document_timeout=900,
                              accelerator_options=AcceleratorOptions(num_threads=threads))
    opts.table_structure_options.mode = TableFormerMode.ACCURATE
    opts.ocr_options = {"ocrmac": OcrMacOptions, "easyocr": EasyOcrOptions, "tesseract": TesseractCliOcrOptions}[settings.ocr_engine]()
    _converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    _converter.initialize_pipeline(InputFormat.PDF)


def _fault(start: int, end: int) -> None:
    """Test-only fault injection. COMPDOC_FAIL_PAGES='45' fails any shard containing page 45, always;
    COMPDOC_FAIL_ONCE='45' fails it only on the first attempt (a marker file remembers)."""
    for var, once in (("COMPDOC_FAIL_PAGES", False), ("COMPDOC_FAIL_ONCE", True)):
        for p in filter(None, os.environ.get(var, "").split(",")):
            if start <= int(p) <= end:
                marker = settings.cache_dir / f".fail_once_{p}"
                if once and marker.exists():
                    continue
                if once:
                    marker.touch()
                raise RuntimeError(f"injected failure on page {p}")


def _convert_range(path: str, start: int, end: int, out_path: str) -> dict:
    """Worker task: convert one page range, save its DoclingDocument JSON to the shard cache."""
    _fault(start, end)
    t0 = time.perf_counter()
    res = _converter.convert(path, page_range=(start, end))
    res.document.save_as_json(Path(out_path))
    return {"seconds": round(time.perf_counter() - t0, 2), "pid": os.getpid(),
            "status": getattr(res.status, "value", str(res.status)), "texts": len(res.document.texts)}


_pool: ProcessPoolExecutor | None = None


def pool() -> ProcessPoolExecutor:
    """Warm pool, created once per coordinator process and reused across documents."""
    global _pool
    if _pool is None:
        import multiprocessing as mp
        threads = max(1, (os.cpu_count() or 2) // WORKERS)
        _pool = ProcessPoolExecutor(max_workers=WORKERS, mp_context=mp.get_context("spawn"), initializer=_init_worker,
                                    initargs=(threads,))
    return _pool


def shutdown_pool() -> None:
    global _pool
    if _pool:
        _pool.shutdown(cancel_futures=True)
        _pool = None


# ------------------------------------------------------------------------------------------ coordinator
def shard_cache_path(fhash: str, s: Shard) -> Path:
    d = settings.cache_dir / "shards"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{fhash}_{s.start}-{s.end}_{pipeline_version()}.docling.json"


def convert_sharded(path: str, fhash: str, on_shard=None) -> tuple[DoclingDocument, dict]:
    """Convert a large PDF in parallel shards; returns the merged document and a shard report for QC."""
    t0 = time.perf_counter()
    pages = probe(path)
    n = len(pages)
    plan = plan_shards(pages)
    report = {"pages": n, "scanned_pages": sum(p["scanned"] for p in pages), "shard_pages": SHARD_PAGES, "workers": WORKERS,
              "pipeline_version": pipeline_version(), "shards": [], "failed_pages": []}
    done: list[tuple[Shard, Path]] = []
    queue: list[Shard] = list(plan)
    running: dict[Future, tuple[Shard, Path]] = {}
    ex = pool()

    def submit(s: Shard) -> None:
        out = shard_cache_path(fhash, s)
        if out.exists():
            s.status = "cached"
            done.append((s, out))
            if on_shard:
                on_shard(s)
            return
        s.attempts += 1
        running[ex.submit(_convert_range, path, s.start, s.end, str(out))] = (s, out)

    for s in queue:
        submit(s)
    while running:
        fut = next(iter(running))
        s, out = running.pop(fut)
        try:
            r = fut.result()
            s.status, s.seconds, s.worker, s.docling_status = "done", r["seconds"], r["pid"], r["status"]
            done.append((s, out))
        except Exception as e:  # noqa: BLE001 - retry, then split, then give up on single pages
            s.error = f"{type(e).__name__}: {e}"[:300]
            if s.attempts <= MAX_RETRIES:
                submit(s)
                continue
            if s.pages > 1:
                mid = (s.start + s.end) // 2
                halves = [Shard(s.start, mid, s.scanned), Shard(mid + 1, s.end, s.scanned)]
                s.status, s.children = "split", [f"{h.start}-{h.end}" for h in halves]
                report["shards"].append(asdict(s))
                for h in halves:
                    submit(h)
                continue
            s.status = "failed"
            report["failed_pages"].append(s.start)
        if on_shard:
            on_shard(s)
        if s.status != "split":
            report["shards"].append(asdict(s))
    report["shards"] += [asdict(s) for s, _ in done if s.status == "cached"]
    # merge in page order
    done.sort(key=lambda x: x[0].start)
    docs = [DoclingDocument.load_from_json(p) for _, p in done]
    merged = DoclingDocument.concatenate(docs) if len(docs) > 1 else docs[0]
    merged.name = Path(path).stem
    covered = sorted(merged.pages)
    missing = sorted(set(range(1, n + 1)) - set(covered))
    report.update(pages_converted=len(covered), pages_missing=missing, seconds=round(time.perf_counter() - t0, 2),
                  status="success" if not missing and not report["failed_pages"] else "partial_success")
    report["shards"].sort(key=lambda x: x["start"])
    return merged, report


def should_shard(path: str) -> bool:
    if not path.lower().endswith(".pdf") or WORKERS < 2:
        return False
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(path)
    n = len(pdf)
    pdf.close()
    return n >= SHARD_MIN_PAGES
