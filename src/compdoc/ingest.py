"""Digitization: source DB rows -> Docling -> agent-facing Sections (+ QC, cache, enrichment).

Steps per document (see plan, "Digitization pipeline"):
 1. register      read the source DB (manifest.json mock) into `documents`
 2. convert       Docling DocumentConverter (layout + TableFormer + OCR for scans), cached by file hash
 3. sections      rebuild the clause tree from Docling items (numbering-based nesting + heading repair)
 4. store         sections table, docling JSON + markdown in var/cache, ingest QC row
 5. enrich        Digitization Agent (LLM) adds summaries, term tags, definitions, cross refs, scope notes
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from docling_core.types.doc import DoclingDocument

from .config import settings
from .db import audit, j, q, q1, tx
from .observability import span
from .schemas import Section

NOISE = [re.compile(p, re.I) for p in (r"^confidential", r"^signed for and on behalf", r"^page \d+( of \d+)?$", r"^_{5,}")]
NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+(\S.*)$")


# ------------------------------------------------------------------------------------------ 1. register
def register_from_manifest(source_dir: Path | None = None) -> int:
    """Mock of the firm's document DB: load manifest.json rows into `documents`."""
    source_dir = source_dir or settings.source_dir
    rows = json.loads((source_dir / "manifest.json").read_text())
    with tx() as c:
        for r in rows:
            path = str((source_dir / r["path"]).resolve())
            c.execute(
                "INSERT INTO documents(doc_id, trader_id, trader_name, title, doc_type, doc_date, path, format)"
                " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(doc_id) DO UPDATE SET trader_id=excluded.trader_id,"
                " trader_name=excluded.trader_name, title=excluded.title, doc_type=excluded.doc_type,"
                " doc_date=excluded.doc_date, path=excluded.path, format=excluded.format",
                (r["doc_id"], r["trader_id"], r.get("trader_name"), r["title"], r["doc_type"], r["doc_date"], path, r.get("format")))
    return len(rows)


def file_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:24]


# ------------------------------------------------------------------------------------------ 2. convert
@lru_cache(maxsize=1)
def _converter():
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import EasyOcrOptions, OcrMacOptions, PdfPipelineOptions, TableFormerMode, TesseractCliOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions(do_ocr=True, do_table_structure=True)
    opts.table_structure_options.mode = TableFormerMode.ACCURATE
    opts.ocr_options = {"ocrmac": OcrMacOptions, "easyocr": EasyOcrOptions, "tesseract": TesseractCliOcrOptions}[settings.ocr_engine]()
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})


def pipeline_options() -> dict:
    """The Docling settings used for conversion (shown in the Dev Console)."""
    return {"do_ocr": True, "ocr_engine": settings.ocr_engine, "do_table_structure": True, "table_mode": "ACCURATE",
            "pdf_backend": "docling layout model + TableFormer", "docx_backend": "python-docx declarative (no models)"}


def convert(path: str, fhash: str) -> tuple[DoclingDocument, dict]:
    """Docling conversion with a file-hash cache. Returns (document, conversion row)."""
    cached = q1("SELECT * FROM conversions WHERE file_hash=?", (fhash,))
    if cached and cached["docling_json_path"] and Path(cached["docling_json_path"]).exists():
        with span("node", "docling:cache_hit", input={"file_hash": fhash}):
            return DoclingDocument.load_from_json(Path(cached["docling_json_path"])), cached
    with span("node", "docling:convert", input={"path": Path(path).name}) as s:
        t0 = time.perf_counter()
        res = _converter().convert(path)
        secs = round(time.perf_counter() - t0, 2)
        doc = res.document
        jpath = settings.cache_dir / f"{fhash}.docling.json"
        mpath = settings.cache_dir / f"{fhash}.md"
        doc.save_as_json(jpath)
        mpath.write_text(doc.export_to_markdown())
        row = {"file_hash": fhash, "status": res.status.value if hasattr(res.status, "value") else str(res.status),
               "docling_json_path": str(jpath), "markdown_path": str(mpath), "seconds": secs}
        s.output = {**row, "items": len(doc.texts), "tables": len(doc.tables), "pages": len(doc.pages),
                    "pipeline": pipeline_options()}
    return doc, row


# ------------------------------------------------------------------------------------------ 3. sections
def _is_noise(text: str) -> bool:
    return any(p.search(text.strip()) for p in NOISE)


def _looks_like_heading(item, text: str) -> bool:
    """Docling sometimes labels a numbered heading as a list item and strips the number into `marker`."""
    if item.label.value != "list_item":
        return False
    words = text.split()
    return 0 < len(words) <= 6 and not text.rstrip().endswith((".", ";", ":")) and text[:1].isupper()


def _prov(item) -> tuple[list[int], list[dict]]:
    pages, boxes = [], []
    for p in getattr(item, "prov", []) or []:
        pages.append(p.page_no)
        b = p.bbox
        boxes.append({"page": p.page_no, "l": b.l, "t": b.t, "r": b.r, "b": b.b,
                      "origin": getattr(b.coord_origin, "value", str(b.coord_origin))})
    return pages, boxes


def build_sections(doc_id: str, doc: DoclingDocument) -> tuple[list[Section], dict]:
    """Turn Docling's (mostly flat) item list into a numbered clause tree."""
    raw: list[dict] = []  # headings with attached content, in reading order
    cur: dict | None = None
    repaired = 0
    decisions: list[dict] = []  # every builder decision, shown in the Dev Console document lifecycle
    for item, _lvl in doc.iterate_items():
        label = item.label.value
        if label in ("page_header", "page_footer", "picture"):
            decisions.append({"step": "drop", "ref": item.self_ref, "label": label, "text": (getattr(item, "text", "") or "")[:120],
                              "reason": f"layout element '{label}' is not contract content"})
            continue
        if label == "table":
            if cur is None:
                cur = {"heading": "Preamble", "number": None, "kind": "preamble", "items": []}
                raw.append(cur)
            cur["items"].append(("table", item.export_to_markdown(doc=doc), item))
            continue
        text = (getattr(item, "text", "") or "").strip()
        if not text or _is_noise(text):
            if text:
                decisions.append({"step": "drop", "ref": item.self_ref, "label": label, "text": text[:120],
                                  "reason": "boilerplate (confidentiality line / signature block / page number)"})
            continue
        is_heading = label in ("section_header", "title") or _looks_like_heading(item, text)
        if is_heading:
            m = NUM_RE.match(text)
            number, heading = (m.group(1), m.group(2)) if m else (None, text)
            how = f"Docling '{label}', number parsed from text" if m else f"Docling '{label}'"
            if number is None and label == "list_item":
                marker = (getattr(item, "marker", "") or "").strip().rstrip(".")
                if re.fullmatch(r"\d+(\.\d+)*", marker):
                    number = marker
                how = "Docling mislabelled a heading as a list item - promoted to heading" + \
                      (f", number '{number}' recovered from list marker" if number else ", number lost")
                repaired += 1
            decisions.append({"step": "heading", "ref": item.self_ref, "label": label, "text": text[:120],
                              "number": number, "reason": how})
            kind = "title" if (label == "title" or (not raw and number is None)) else "heading"
            docx_level = getattr(item, "level", None) if label == "section_header" else None
            cur = {"heading": heading, "number": number, "kind": kind, "items": [], "docx_level": docx_level, "item": item}
            raw.append(cur)
        else:
            if cur is None:
                cur = {"heading": "Preamble", "number": None, "kind": "preamble", "items": []}
                raw.append(cur)
            cur["items"].append(("text", text, item))

    # Heading repair: give unnumbered headings a number from their numbered neighbours.
    seen = {h["number"] for h in raw if h["number"]}
    for i, h in enumerate(raw):
        if h["number"] or h["kind"] != "heading":
            continue
        nxt = next((x["number"] for x in raw[i + 1:] if x["number"]), None)
        prev = next((x["number"] for x in reversed(raw[:i]) if x["number"]), None)
        guess = None
        if nxt and "." in nxt:
            top, sub = nxt.split(".", 1)
            if top not in seen:
                guess = top
            elif sub.isdigit() and int(sub) > 1 and prev == top:
                guess = f"{top}.{int(sub) - 1}"
        elif nxt and nxt.isdigit() and prev and prev.split(".")[0].isdigit() and int(nxt) - int(prev.split(".")[0]) == 2:
            guess = str(int(nxt) - 1)
        elif nxt is None and prev:
            guess = str(int(prev.split(".")[0]) + 1)
        if guess and guess not in seen:
            h["number"] = guess
            h["repaired"] = True
            seen.add(guess)
            decisions.append({"step": "repair", "ref": getattr(h.get("item"), "self_ref", None), "text": h["heading"],
                              "number": guess, "reason": f"unnumbered heading between {prev or '-'} and {nxt or '-'} - assigned {guess}"})

    # Fold the document title into the preamble so the preamble keeps the doc's opening text.
    sections: list[Section] = []
    by_number: dict[str, Section] = {}
    title = next((h["heading"] for h in raw if h["kind"] == "title"), None)
    for ordn, h in enumerate(raw):
        number = h["number"]
        if h["kind"] in ("title", "preamble"):
            sid, heading, level, parent_id, path = f"{doc_id}:s0", "Preamble", 0, None, ["Preamble"]
            if sections and sections[0].section_id == sid:  # merge title + preamble
                sec = sections[0]
                _attach(sec, h["items"])
                continue
            heading = title or "Preamble"
        else:
            sid = f"{doc_id}:s{number}" if number else f"{doc_id}:x{ordn}"
            heading = h["heading"]
            level = number.count(".") + 1 if number else (h.get("docx_level") or 1)
            parent_num = number.rsplit(".", 1)[0] if number and "." in number else None
            parent = by_number.get(parent_num) if parent_num else None
            parent_id = parent.section_id if parent else None
            path = (parent.path if parent else []) + [h["heading"]]
        sec = Section(section_id=sid, doc_id=doc_id, number=number, heading=heading, path=path, level=level, parent_id=parent_id)
        if h.get("item") is not None:
            pg, bx = _prov(h["item"])
            sec.pages += pg
            sec.bboxes += bx
            sec.item_refs.append(h["item"].self_ref)
        _attach(sec, h["items"])
        sections.append(sec)
        if number:
            by_number[number] = sec
    stats = {"sections": len(sections), "headings_repaired": repaired,
             "unnumbered_after_repair": sum(1 for s in sections if s.level > 0 and not s.number),
             "decisions": decisions}
    return sections, stats


def _attach(sec: Section, items: list) -> None:
    texts = [sec.text] if sec.text else []
    for kind, payload, item in items:
        if kind == "table":
            sec.tables_md.append(payload)
        else:
            texts.append(payload)
        pg, bx = _prov(item)
        sec.pages += pg
        sec.bboxes += bx
        sec.item_refs.append(item.self_ref)
    sec.text = "\n".join(texts)
    sec.pages = sorted(set(sec.pages))


def subtree_text(sections: list[Section], sec: Section) -> str:
    """Section text plus its descendants (for leaf sections this is just its own text)."""
    out = [sec.text] + sec.tables_md
    for child in (s for s in sections if s.parent_id == sec.section_id):
        out.append(f"{child.number or ''} {child.heading}\n{subtree_text(sections, child)}")
    return "\n".join(x for x in out if x)


# ------------------------------------------------------------------------------------------ 4. QC + store
def qc_for(doc: DoclingDocument, row: dict, fmt: str | None, stats: dict) -> dict:
    page_chars: dict[int, int] = {}
    for t in doc.texts:
        for p in t.prov or []:
            page_chars[p.page_no] = page_chars.get(p.page_no, 0) + len(t.text or "")
    page_sizes = {str(k): {"w": v.size.width, "h": v.size.height} for k, v in (doc.pages or {}).items()}
    qc = {"status": row.get("status"), "pages": len(doc.pages), "tables": len(doc.tables), "text_items": len(doc.texts),
          "ocr_used": fmt == "scan", "low_text_pages": [p for p, n in page_chars.items() if n < 200],
          "page_sizes": page_sizes, "seconds": row.get("seconds"), **stats}
    flags = []
    if str(qc["status"]).lower() not in ("success", "conversionstatus.success"):
        flags.append("conversion_not_success")
    if stats["sections"] <= 1:
        flags.append("no_sections")
    if stats["unnumbered_after_repair"]:
        flags.append("unnumbered_headings")
    qc["flags"] = flags
    return qc


def store_sections(doc_id: str, sections: list[Section]) -> None:
    with tx() as c:
        old = {r["section_id"]: r for r in c.execute("SELECT * FROM sections WHERE doc_id=?", (doc_id,)).fetchall()}
        c.execute("DELETE FROM sections WHERE doc_id=?", (doc_id,))
        for i, s in enumerate(sections):
            o = old.get(s.section_id)
            c.execute(
                "INSERT INTO sections(section_id, doc_id, ord, number, heading, path_json, level, parent_id, pages_json, text,"
                " tables_json, item_refs_json, bboxes_json, summary, term_tags_json, defined_terms_json, cross_refs_json, scope_notes_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (s.section_id, doc_id, i, s.number, s.heading, j(s.path), s.level, s.parent_id, j(s.pages), s.text,
                 j(s.tables_md), j(s.item_refs), j(s.bboxes),
                 o["summary"] if o else None, o["term_tags_json"] if o else None, o["defined_terms_json"] if o else None,
                 o["cross_refs_json"] if o else None, o["scope_notes_json"] if o else None))


def ingest_document(doc_id: str, enrich: bool = True, force: bool = False) -> dict:
    d = q1("SELECT * FROM documents WHERE doc_id=?", (doc_id,))
    if not d:
        raise KeyError(doc_id)
    with span("node", f"ingest:{doc_id}", input={"doc_id": doc_id, "path": Path(d["path"]).name}) as s:
        fh = file_hash(d["path"])
        unchanged = d["file_hash"] == fh and q1("SELECT 1 FROM sections WHERE doc_id=? LIMIT 1", (doc_id,))
        if unchanged and not force:
            result = {"doc_id": doc_id, "skipped": "unchanged"}
        else:
            doc, row = convert(d["path"], fh)
            sections, stats = build_sections(doc_id, doc)
            qc = qc_for(doc, row, d["format"], stats)
            store_sections(doc_id, sections)
            with tx() as c:
                c.execute("INSERT INTO conversions(file_hash, status, qc_json, docling_json_path, markdown_path, converted_at, seconds)"
                          " VALUES (?,?,?,?,?,?,?) ON CONFLICT(file_hash) DO UPDATE SET qc_json=excluded.qc_json, status=excluded.status",
                          (fh, row["status"], j(qc), row["docling_json_path"], row["markdown_path"],
                           datetime.now(timezone.utc).isoformat(), row.get("seconds")))
                c.execute("UPDATE documents SET file_hash=?, ingested_at=? WHERE doc_id=?",
                          (fh, datetime.now(timezone.utc).isoformat(), doc_id))
            result = {"doc_id": doc_id, "sections": len(sections), "qc_flags": qc["flags"]}
        if enrich:
            from .agents.digitizer import enrich_document
            result["enrichment"] = enrich_document(doc_id, force=force)
        s.output = result
    audit("ingest", "document_ingested", d["trader_id"], doc_id=doc_id, result=result)
    return result


def load_sections(doc_id: str) -> list[Section]:
    rows = q("SELECT * FROM sections WHERE doc_id=? ORDER BY ord", (doc_id,))
    return [Section(section_id=r["section_id"], doc_id=r["doc_id"], number=r["number"], heading=r["heading"],
                    path=json.loads(r["path_json"]), level=r["level"], parent_id=r["parent_id"],
                    pages=json.loads(r["pages_json"]), text=r["text"], tables_md=json.loads(r["tables_json"]),
                    item_refs=json.loads(r["item_refs_json"]), bboxes=json.loads(r["bboxes_json"])) for r in rows]
