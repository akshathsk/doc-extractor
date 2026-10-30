"""Document tools: inventory, outline, read section, find sections by term tag."""
from __future__ import annotations

import json

from ..db import q, q1
from .access import require


def _trader_of_doc(doc_id: str) -> str:
    d = q1("SELECT trader_id FROM documents WHERE doc_id=?", (doc_id,))
    if not d:
        raise KeyError(f"unknown doc_id {doc_id}")
    return d["trader_id"]


def effective_date(doc_id: str) -> str | None:
    r = q1("SELECT d.doc_date, e.effective_date_in_text FROM documents d LEFT JOIN doc_enrichment e USING(doc_id)"
           " WHERE d.doc_id=?", (doc_id,))
    return (r["effective_date_in_text"] or r["doc_date"]) if r else None


def list_trader_docs(trader_id: str, caller: str = "system") -> list[dict]:
    require(caller, trader_id)
    rows = q("SELECT d.doc_id, d.title, d.doc_type, d.doc_date, d.format, e.doc_summary, e.terms_covered_json,"
             " e.effective_date_in_text, e.doc_scope_note, c.qc_json FROM documents d"
             " LEFT JOIN doc_enrichment e USING(doc_id) LEFT JOIN conversions c ON c.file_hash=d.file_hash"
             " WHERE d.trader_id=? ORDER BY d.doc_date, d.doc_id", (trader_id,))
    out = []
    for r in rows:
        qc = json.loads(r.pop("qc_json") or "{}")
        r["terms_covered"] = json.loads(r.pop("terms_covered_json") or "[]")
        r["qc_flags"] = qc.get("flags", [])
        out.append(r)
    return out


def get_section_outline(doc_id: str, caller: str = "system") -> list[dict]:
    require(caller, _trader_of_doc(doc_id))
    rows = q("SELECT section_id, number, heading, level, parent_id, pages_json, summary, term_tags_json FROM sections"
             " WHERE doc_id=? ORDER BY ord", (doc_id,))
    return [{"section_id": r["section_id"], "number": r["number"], "heading": r["heading"], "level": r["level"],
             "parent_id": r["parent_id"], "pages": json.loads(r["pages_json"]), "summary": r["summary"],
             "term_tags": json.loads(r["term_tags_json"] or "[]")} for r in rows]


def read_section(doc_id: str, section_id: str, caller: str = "system") -> dict:
    """Full section text (+ children + tables) with page numbers, plus definitions and cross-referenced clauses."""
    require(caller, _trader_of_doc(doc_id))
    s = q1("SELECT * FROM sections WHERE section_id=? AND doc_id=?", (section_id, doc_id))
    if not s:
        raise KeyError(f"section {section_id} not found in {doc_id}")
    children = q("SELECT section_id, number, heading, text, tables_json FROM sections WHERE parent_id=? ORDER BY ord", (section_id,))
    doc = q1("SELECT doc_id, trader_id, title, doc_type, doc_date FROM documents WHERE doc_id=?", (doc_id,))
    # definitions from the same document (Definitions section) referenced in this text
    text_all = s["text"] + " ".join(c["text"] for c in children)
    defs = []
    for d in q("SELECT text FROM sections WHERE doc_id=? AND lower(heading) LIKE '%definition%'", (doc_id,)):
        for line in d["text"].split("\n"):
            term = line.split('"')[1] if line.count('"') >= 2 else None
            if term and term.lower() in text_all.lower():
                defs.append(line)
    xrefs = []
    for ref in json.loads(s["cross_refs_json"] or "[]")[:3]:
        r = q1("SELECT section_id, heading, text FROM sections WHERE doc_id=? AND number=?", (doc_id, ref))
        if r:
            xrefs.append({"section_id": r["section_id"], "heading": r["heading"], "text": r["text"][:600]})
    return {
        "doc": doc, "effective_date": effective_date(doc_id),
        "section_id": section_id, "number": s["number"], "heading": s["heading"], "path": json.loads(s["path_json"]),
        "pages": json.loads(s["pages_json"]), "text": s["text"], "tables": json.loads(s["tables_json"]),
        "children": [{"section_id": c["section_id"], "number": c["number"], "heading": c["heading"], "text": c["text"],
                      "tables": json.loads(c["tables_json"])} for c in children],
        "scope_notes": json.loads(s["scope_notes_json"] or "[]"),
        "definitions": defs, "cross_referenced": xrefs,
    }


def find_sections_by_term(trader_id: str, term_path: str, caller: str = "system") -> list[dict]:
    """Sections tagged (by enrichment) with a term or sub-term path, across all of the trader's documents."""
    require(caller, trader_id)
    rows = q("SELECT s.section_id, s.doc_id, s.number, s.heading, s.summary, s.term_tags_json, d.doc_date, d.title"
             " FROM sections s JOIN documents d USING(doc_id) WHERE d.trader_id=? ORDER BY d.doc_date, s.ord", (trader_id,))
    out = []
    for r in rows:
        tags = json.loads(r["term_tags_json"] or "[]")
        if any(t == term_path or t.startswith(term_path + ".") for t in tags):
            out.append({"doc_id": r["doc_id"], "doc_title": r["title"], "doc_date": r["doc_date"], "section_id": r["section_id"],
                        "number": r["number"], "heading": r["heading"], "summary": r["summary"], "term_tags": tags})
    return out


def get_section_outlines(doc_ids: list[str], caller: str = "system") -> list[dict]:
    """Outlines of several documents at once: [{doc_id, title, doc_date, outline:[...]}]. Body text is not included."""
    out = []
    for did in doc_ids:
        d = q1("SELECT doc_id, title, doc_type, doc_date FROM documents WHERE doc_id=?", (did,))
        if not d:
            out.append({"doc_id": did, "error": "unknown doc_id"})
            continue
        outline = [{k: o[k] for k in ("section_id", "number", "heading", "level", "summary", "term_tags")}
                   for o in get_section_outline(did, caller) if o["level"] > 0]
        out.append({**d, "outline": outline})
    return out


def read_chosen_sections(choices: list[dict], caller: str = "system") -> list[dict]:
    """Read the sections chosen from the outlines. Each choice carries the reason it was chosen (kept in the trace)."""
    out = []
    for c in choices:
        try:
            sec = read_section(c["doc_id"], c["section_id"], caller)
            out.append({"reason": c.get("reason"), **sec})
        except (KeyError, Exception) as e:  # noqa: BLE001
            out.append({"doc_id": c.get("doc_id"), "section_id": c.get("section_id"), "error": str(e)})
    return out


def _descendants(doc_id: str) -> dict[str, list[dict]]:
    rows = q("SELECT section_id, parent_id, number, heading, level, pages_json, summary, term_tags_json FROM sections "
             "WHERE doc_id=? ORDER BY ord", (doc_id,))
    kids: dict[str, list[dict]] = {}
    for r in rows:
        if r["parent_id"]:
            kids.setdefault(r["parent_id"], []).append(r)
    return {"rows": rows, "kids": kids}  # type: ignore[return-value]


def get_article_outline(doc_id: str, caller: str = "system") -> list[dict]:
    """Level 1 of the two-level outline: one line per article / schedule / part, with its summary and the term tags
    of everything inside it. ~15-30 lines even for a 200-page agreement."""
    require(caller, _trader_of_doc(doc_id))
    d = _descendants(doc_id)
    arts = {r["section_id"]: r["summary"] for r in q("SELECT section_id, summary FROM article_enrichment WHERE doc_id=?", (doc_id,))}

    def walk(sid: str) -> list[dict]:
        out = []
        for c in d["kids"].get(sid, []):
            out.append(c)
            out += walk(c["section_id"])
        return out
    outline = []
    for r in d["rows"]:
        if r["level"] != 1:
            continue
        inside = walk(r["section_id"])
        tags = sorted({t for x in [r] + inside for t in json.loads(x["term_tags_json"] or "[]")})
        outline.append({"section_id": r["section_id"], "number": r["number"], "heading": r["heading"],
                        "sections_inside": len(inside), "pages": json.loads(r["pages_json"])[:1] + json.loads(inside[-1]["pages_json"])[-1:] if inside else json.loads(r["pages_json"]),
                        "summary": ((arts.get(r["section_id"]) or r["summary"] or "")[:180]), "term_tags": tags})
    return outline


def get_article_sections(doc_id: str, article_id: str, caller: str = "system") -> list[dict]:
    """Level 2: the sections inside one article (number, heading, summary, tags) - no body text."""
    require(caller, _trader_of_doc(doc_id))
    d = _descendants(doc_id)

    def walk(sid: str, depth: int) -> list[dict]:
        out = []
        for c in d["kids"].get(sid, []):
            out.append({"section_id": c["section_id"], "number": c["number"], "heading": c["heading"], "depth": depth,
                        "summary": c["summary"], "term_tags": json.loads(c["term_tags_json"] or "[]")})
            out += walk(c["section_id"], depth + 1)
        return out
    if not any(r["section_id"] == article_id for r in d["rows"]):
        raise KeyError(f"{article_id} not in {doc_id}")
    return walk(article_id, 1)
