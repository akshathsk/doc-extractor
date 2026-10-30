"""verify_quote: the quote must appear in the cited section's Docling-derived text, and contain the value."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from ..db import q1


def norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("P&L;", "P&L").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip().lower().strip(" .;:")


def _value_forms(value) -> list[str]:
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        v = int(value) if float(value).is_integer() else value
        return [f"{v:,}", str(v)]
    return [str(value).lower()]


def _docling_text(doc_id: str) -> str:
    r = q1("SELECT c.docling_json_path FROM documents d JOIN conversions c ON c.file_hash=d.file_hash WHERE d.doc_id=?", (doc_id,))
    if not r or not r["docling_json_path"] or not Path(r["docling_json_path"]).exists():
        return ""
    data = json.loads(Path(r["docling_json_path"]).read_text())
    parts = [t.get("text", "") for t in data.get("texts", [])]
    for t in data.get("tables", []):
        for cell in t.get("data", {}).get("table_cells", []):
            parts.append(cell.get("text", ""))
    return " ".join(parts)


def verify_quote(doc_id: str, section_id: str, quote: str, value=None) -> dict:
    sec = q1("SELECT text, tables_json FROM sections WHERE section_id=? AND doc_id=?", (section_id, doc_id))
    if not sec:
        return {"verified": False, "reason": f"section {section_id} not in {doc_id}"}
    nq = norm(quote)
    if len(nq) < 8:
        return {"verified": False, "reason": "quote too short"}
    children = q1("SELECT group_concat(text, ' ') t FROM sections WHERE parent_id=?", (section_id,))
    sec_text = norm(sec["text"] + " " + " ".join(json.loads(sec["tables_json"])) + " " + ((children or {}).get("t") or ""))
    in_section = nq in sec_text
    in_doc = in_section or nq in norm(_docling_text(doc_id))
    forms = _value_forms(value)
    value_ok = not forms or any(f.lower() in nq for f in forms)
    verified = in_section and value_ok
    reason = "ok" if verified else ("value not in quote" if in_section else
                                     ("quote found in document but not in cited section" if in_doc else "quote not found"))
    return {"verified": verified, "in_section": in_section, "in_document": in_doc, "value_in_quote": value_ok, "reason": reason}


@lru_cache(maxsize=64)
def _docling_items(doc_id: str) -> dict:
    """self_ref -> {text, prov} for the document's cached DoclingDocument."""
    r = q1("SELECT c.docling_json_path FROM documents d JOIN conversions c ON c.file_hash=d.file_hash WHERE d.doc_id=?", (doc_id,))
    if not r or not r["docling_json_path"] or not Path(r["docling_json_path"]).exists():
        return {}
    data = json.loads(Path(r["docling_json_path"]).read_text())
    return {t["self_ref"]: {"text": t.get("text", ""), "prov": t.get("prov", [])} for t in data.get("texts", [])}


def locate_quote(doc_id: str, section_id: str, quote: str) -> dict:
    """Find the Docling item inside the cited section that contains the quote; return its ref, page and bbox."""
    sec = q1("SELECT item_refs_json FROM sections WHERE section_id=?", (section_id,))
    items = _docling_items(doc_id)
    if not sec or not items:
        return {}
    nq = norm(quote)
    refs = json.loads(sec["item_refs_json"] or "[]")
    kids = q1("SELECT group_concat(item_refs_json, '|') r FROM sections WHERE parent_id=?", (section_id,))
    for part in ((kids or {}).get("r") or "").split("|"):
        refs += json.loads(part) if part else []
    best = None
    for ref in refs:
        it = items.get(ref)
        if not it:
            continue
        nt = norm(it["text"])
        if nq and (nq in nt or (len(nt) > 20 and nt in nq)):
            best = (ref, it)
            break
    if not best:
        return {}
    ref, it = best
    prov = (it["prov"] or [{}])[0]
    bb = prov.get("bbox") or {}
    return {"docling_ref": ref, "page": prov.get("page_no"),
            "bbox": {"page": prov.get("page_no"), "l": bb.get("l"), "t": bb.get("t"), "r": bb.get("r"), "b": bb.get("b"),
                     "origin": bb.get("coord_origin")} if bb else None}
