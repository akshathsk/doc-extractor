"""Quote verification and location (scaling plan, Part 4).

verify_quote: the quote must appear in the cited section (or its child clauses) and contain the value.
locate_quote: which Docling item(s), page(s) and bounding box(es) the quote sits in.

Both read only the text store written at ingest (`sections`, `section_text`, `section_spans`); Docling JSON is
never opened while agents run. Matching is exact first, then OCR-tolerant (hyphenated line breaks, punctuation).
"""
from __future__ import annotations

import json
import re

from ..db import q, q1

_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def norm(s: str) -> str:
    s = (s or "").translate(_QUOTES).replace("P&L;", "P&L").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip().lower().strip(" .;:")


def tolerant(s: str) -> str:
    """OCR-tolerant form: join words hyphenated across line breaks, drop punctuation, collapse spaces."""
    s = (s or "").translate(_QUOTES).replace("P&L;", "P&L")
    s = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", s)
    s = re.sub(r"[^\w%$]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _norm_with_map(raw: str, mode: str) -> tuple[str, list[int]]:
    """Normalised text plus, for each normalised character, its index in `raw` (so a match maps back to spans)."""
    raw = (raw or "").translate(_QUOTES)
    out: list[str] = []
    idx: list[int] = []
    prev_space = True
    i = 0
    while i < len(raw):
        ch = raw[i]
        if mode == "tolerant" and ch == "-" and i + 1 < len(raw) and raw[i + 1] == "\n":
            i += 2  # hyphen at a line break: join the word
            continue
        c = ch.lower()
        is_space = c.isspace() or (mode == "tolerant" and not (c.isalnum() or c in "%$_"))
        if is_space:
            if not prev_space:
                out.append(" ")
                idx.append(i)
            prev_space = True
        else:
            out.append(c)
            idx.append(i)
            prev_space = False
        i += 1
    return "".join(out), idx


def _value_forms(value) -> list[str]:
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        v = int(value) if float(value).is_integer() else value
        return [f"{v:,}", str(v)]
    return [str(value).lower()]


def _section_family(section_id: str) -> list[dict]:
    """The section and its direct child clauses (a quote citing 4.2 may sit in 4.2(a))."""
    return q("SELECT section_id, text, tables_json FROM sections WHERE section_id=? OR parent_id=? ORDER BY ord", (section_id, section_id))


def verify_quote(doc_id: str, section_id: str, quote: str, value=None) -> dict:
    fam = [r for r in _section_family(section_id)]
    if not any(r["section_id"] == section_id for r in fam) or not q1("SELECT 1 FROM sections WHERE section_id=? AND doc_id=?",
                                                                      (section_id, doc_id)):
        return {"verified": False, "reason": f"section {section_id} not in {doc_id}"}
    nq = norm(quote)
    if len(nq) < 8:
        return {"verified": False, "reason": "quote too short"}
    blob = " ".join(r["text"] + " " + " ".join(json.loads(r["tables_json"] or "[]")) for r in fam)
    match = "exact" if nq in norm(blob) else ("tolerant" if tolerant(quote) and tolerant(quote) in tolerant(blob) else None)
    found_in: list[str] = []
    if not match:  # where else in the document? (text store, no Docling JSON)
        tq = tolerant(quote)
        for r in q("SELECT section_id, norm_text FROM section_text WHERE doc_id=?", (doc_id,)):
            if nq in r["norm_text"] or (tq and tq in tolerant(r["norm_text"])):
                found_in.append(r["section_id"])
    forms = _value_forms(value)
    value_ok = not forms or any(f.lower() in nq for f in forms) or any(f.lower() in tolerant(quote) for f in forms)
    verified = bool(match) and value_ok
    if verified:
        reason = "ok" if match == "exact" else "ok (OCR-tolerant match)"
    elif match:
        reason = "value not in quote"
    elif found_in:
        reason = f"quote found in another section of the document: {', '.join(found_in[:3])}"
    else:
        reason = "quote not found"
    return {"verified": verified, "match": match, "in_section": bool(match), "in_document": bool(match) or bool(found_in),
            "found_in": found_in[:5], "value_in_quote": value_ok, "reason": reason}


def locate_quote(doc_id: str, section_id: str, quote: str) -> dict:
    """Docling item(s), page(s) and box(es) covering the quote; several boxes when it crosses items or pages."""
    for mode in ("exact", "tolerant"):
        nq = norm(quote) if mode == "exact" else tolerant(quote)
        if not nq:
            continue
        for r in _section_family(section_id):
            text, idx = _norm_with_map(r["text"], "exact" if mode == "exact" else "tolerant")
            pos = text.find(nq)
            if pos < 0:
                continue
            start, end = idx[pos], idx[min(pos + len(nq), len(idx)) - 1] + 1
            spans = q('SELECT item_ref, page, l, t, r, b, origin, start, "end" FROM section_spans WHERE section_id=? '
                      'AND start < ? AND "end" > ? ORDER BY start, page', (r["section_id"], end, start))
            if not spans:
                return {}
            boxes, refs = [], []
            for s in spans:
                if s["item_ref"] not in refs:
                    refs.append(s["item_ref"])
                if s["page"] is not None:
                    boxes.append({"page": s["page"], "l": s["l"], "t": s["t"], "r": s["r"], "b": s["b"], "origin": s["origin"]})
            first = boxes[0] if boxes else None
            return {"docling_ref": refs[0], "refs": refs, "page": first["page"] if first else None, "bbox": first, "boxes": boxes,
                    "match": mode, "section_id": r["section_id"]}
    return {}
