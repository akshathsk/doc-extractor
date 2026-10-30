"""Render a drafted amendment / side letter to .docx with a change table, precedent refs and NEW LANGUAGE flags."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.shared import Pt, RGBColor

from ..config import settings
from ..db import j, q1, tx
from ..observability import current_run_id

FIRM = "Meridian Ridge Capital LP"


def render_docx(trader_id: str, doc_type: str, title: str, clauses: list[dict], request: str = "",
                caller: str = "system") -> dict:
    t = q1("SELECT trader_name FROM documents WHERE trader_id=? LIMIT 1", (trader_id,))
    name = t["trader_name"] if t else trader_id
    draft_id = f"DRAFT-{uuid.uuid4().hex[:8]}"
    path = settings.drafts_dir / f"{draft_id}_{trader_id}.docx"

    doc = Document()
    wm = doc.add_paragraph()
    r = wm.add_run("DRAFT - FOR REVIEW ONLY - NOT APPROVED")
    r.bold, r.font.size, r.font.color.rgb = True, Pt(14), RGBColor(0xC0, 0x00, 0x00)
    doc.add_heading(f"{title} - {name}", level=0)
    doc.add_paragraph(
        f"This {title} is dated [{date.today().isoformat()}] and amends the Employment and Compensation Agreement between "
        f"{FIRM} (the \"Firm\") and {name} (Trader ID {trader_id}). Except as set out below, the Agreement remains in full force and effect."
        if doc_type == "amendment" else
        f"This side letter dated [{date.today().isoformat()}] is delivered by {FIRM} to {name} (Trader ID {trader_id}) and supplements the Agreement.")

    doc.add_heading("Summary of changes (for reviewer)", level=1)
    tbl = doc.add_table(rows=1, cols=5)
    tbl.style = "Table Grid"
    for i, h in enumerate(["Term", "Current", "Proposed", "Precedent", "New language"]):
        tbl.rows[0].cells[i].text = h
    for c in clauses:
        row = tbl.add_row().cells
        row[0].text = c.get("term_path") or c.get("heading", "")
        row[1].text = "" if c.get("old_value") is None else str(c.get("old_value"))
        row[2].text = "" if c.get("new_value") is None else str(c.get("new_value"))
        row[3].text = f"{c.get('precedent_doc_id')} / {c.get('precedent_section_id')}" if c.get("precedent_doc_id") else "none"
        row[4].text = "YES" if c.get("new_language") else "no"

    doc.add_heading("1. Amendments", level=1)
    for i, c in enumerate(clauses, 1):
        doc.add_heading(f"1.{i} {c.get('heading', '')}", level=2)
        p = doc.add_paragraph()
        run = p.add_run(c.get("text", ""))
        if c.get("new_language"):
            run.font.highlight_color = WD_COLOR_INDEX.YELLOW
            p.add_run("  [NEW LANGUAGE - not from precedent]").bold = True
        if c.get("precedent_doc_id"):
            note = doc.add_paragraph(f"Precedent: {c['precedent_doc_id']} {c.get('precedent_section_id') or ''}")
            note.runs[0].font.size, note.runs[0].italic = Pt(8), True
    doc.add_heading("2. Governing Law", level=1)
    doc.add_paragraph("This document is governed by the laws of the State of New York.")
    doc.save(str(path))

    record = {"draft_id": draft_id, "trader_id": trader_id, "doc_type": doc_type, "title": title, "clauses": clauses,
              "precedent_coverage": round(sum(1 for c in clauses if c.get("precedent_doc_id")) / max(1, len(clauses)), 2),
              "new_language_clauses": [c.get("heading") for c in clauses if c.get("new_language")]}
    with tx() as cx:
        cx.execute("INSERT INTO drafts(draft_id, run_id, trader_id, request, path, draft_json, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                   (draft_id, current_run_id(), trader_id, request, str(path), j(record), "pending_review",
                    datetime.now(timezone.utc).isoformat()))
    return {"draft_id": draft_id, "path": str(path), **{k: record[k] for k in ("precedent_coverage", "new_language_clauses")}}


def load_draft(draft_id: str) -> dict | None:
    r = q1("SELECT * FROM drafts WHERE draft_id=?", (draft_id,))
    if r:
        r["draft"] = json.loads(r.pop("draft_json"))
    return r
