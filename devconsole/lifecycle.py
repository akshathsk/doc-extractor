"""Document lifecycle: one document's journey from the source DB row to "ready for agents", step by step,
with the data stored at each step."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pandas as pd
import streamlit as st

from compdoc.config import settings
from compdoc.db import q, q1


def jl(s):
    try:
        return json.loads(s) if s else None
    except (json.JSONDecodeError, TypeError):
        return s


def _size(p: str | None) -> str:
    if not p or not Path(p).exists():
        return "missing"
    n = Path(p).stat().st_size
    return f"{n / 1024:.1f} KB" if n < 1024 ** 2 else f"{n / 1024 ** 2:.1f} MB"


def step(n: int, title: str, what: str, stored: str):
    box = st.container(border=True)
    box.markdown(f"### Step {n} · {title}")
    box.markdown(what)
    box.caption(f"💾 Stored in: {stored}")
    return box


def _page_image(path: str, page_no: int, boxes: list[dict], page_h: float | None, highlight: set[str] | None = None):
    import pypdfium2 as pdfium
    from PIL import ImageDraw
    pdf = pdfium.PdfDocument(path)
    page = pdf[page_no - 1]
    scale, H = 1.6, page_h or page.get_height()
    img = page.render(scale=scale).to_pil().convert("RGB")
    pdf.close()
    drw = ImageDraw.Draw(img)
    palette = [(220, 30, 30), (30, 120, 220), (30, 160, 60), (200, 120, 0), (140, 60, 200)]
    for i, b in enumerate(boxes):
        if b["page"] != page_no or b.get("l") is None:
            continue
        top, bot = ((H - b["t"]) * scale, (H - b["b"]) * scale) if "BOTTOM" in str(b.get("origin", "")).upper() else (b["t"] * scale, b["b"] * scale)
        col = palette[b.get("color", i) % len(palette)]
        drw.rectangle([b["l"] * scale - 2, min(top, bot) - 2, b["r"] * scale + 2, max(top, bot) + 2], outline=col, width=3)
        if b.get("label"):
            drw.text((b["l"] * scale, min(top, bot) - 13), b["label"], fill=col)
    return img


def render_lifecycle() -> None:
    st.title("Document lifecycle")
    st.caption("How one document goes from the source database to sections the agents can use: every step, what it decided, "
               "and what it stored. Steps 1-7 are code (Docling + section builder), step 8 is the LLM, steps 9-10 make it "
               "searchable and usable.")
    docs = q("SELECT d.*, c.status conv_status, c.qc_json, c.docling_json_path, c.markdown_path, c.converted_at, c.seconds"
             " FROM documents d LEFT JOIN conversions c ON c.file_hash=d.file_hash ORDER BY d.trader_id, d.doc_date")
    if not docs:
        st.info("No documents registered. Run `compdoc ingest`.")
        return
    ids = [d["doc_id"] for d in docs]
    did = st.selectbox("Document", ids, format_func=lambda x: next(
        f"{d['doc_id']} · {d['trader_id']} · {d['doc_date']} · {d['title']} ({d['format']})" for d in docs if d["doc_id"] == x))
    d = next(x for x in docs if x["doc_id"] == did)
    qc = jl(d["qc_json"]) or {}
    is_pdf = d["path"].endswith(".pdf")

    # rebuild Docling doc + builder decisions from the cache (deterministic, cheap)
    ddoc, decisions, built = None, [], []
    if d["docling_json_path"] and Path(d["docling_json_path"]).exists():
        from docling_core.types.doc import DoclingDocument

        from compdoc.ingest import build_sections
        ddoc = DoclingDocument.load_from_json(Path(d["docling_json_path"]))
        built, stats = build_sections(did, ddoc)
        decisions = stats.get("decisions", [])
    secs = q("SELECT * FROM sections WHERE doc_id=? ORDER BY ord", (did,))
    enr = q1("SELECT * FROM doc_enrichment WHERE doc_id=?", (did,))

    flow = ["1 Source row", "2 File hash", "3 Docling", "4 Clean", "5 Headings", "6 Sections", "7 QC", "8 LLM enrich",
            "9 Index", "10 Ready"]
    done = [True, bool(d["file_hash"]), bool(ddoc), bool(ddoc), bool(ddoc), bool(secs), bool(qc), bool(enr), bool(secs), bool(secs and enr)]
    st.markdown(" → ".join(f"{'✅' if ok else '⏳'} {f}" for f, ok in zip(flow, done)))

    # 1 ------------------------------------------------------------------------------------------------
    b = step(1, "Source database row", "The firm's document database says this document exists, which trader it belongs to and its "
             "date. Nothing is read from the file yet. (Here the database is mocked by `manifest.json`.)", "`documents` table")
    b.json({k: d[k] for k in ("doc_id", "trader_id", "trader_name", "title", "doc_type", "doc_date", "format", "path")})

    # 2 ------------------------------------------------------------------------------------------------
    b = step(2, "File fingerprint", "The file is hashed (SHA-256). The hash is the cache key: if the file has not changed, Docling "
             "is never run again for it.", "`documents.file_hash`, `documents.ingested_at`")
    from compdoc.ingest import file_hash
    cur_hash = file_hash(d["path"]) if Path(d["path"]).exists() else None
    c = b.columns(4)
    c[0].metric("Format", d["format"])
    c[1].metric("Size", _size(d["path"]))
    c[2].metric("Hash", (d["file_hash"] or "-")[:12] + "…")
    c[3].metric("File unchanged", "yes" if cur_hash == d["file_hash"] else "NO - will reconvert")
    b.caption(f"Ingested at {d['ingested_at']}")

    # 3 ------------------------------------------------------------------------------------------------
    from compdoc.ingest import pipeline_options
    b = step(3, "Docling conversion", "Docling runs locally. **PDF:** a layout model finds headings, paragraphs, lists and tables; "
             "TableFormer rebuilds table cells; text comes from the PDF (or OCR for scans). **DOCX:** Word structure and styles are "
             "read directly. Output: a `DoclingDocument` - a flat list of items, each with a label and provenance (page, bounding box).",
             f"`conversions` row; `{Path(d['docling_json_path'] or '').name}` ({_size(d['docling_json_path'])}) and "
             f"`{Path(d['markdown_path'] or '').name}` ({_size(d['markdown_path'])}) in `{settings.cache_dir}`")
    if ddoc is None:
        b.warning("Not converted yet.")
    else:
        c = b.columns(5)
        c[0].metric("Status", d["conv_status"])
        c[1].metric("Seconds", d["seconds"])
        c[2].metric("Pages", len(ddoc.pages) or "n/a (DOCX)")
        c[3].metric("Text items", len(ddoc.texts))
        c[4].metric("Tables", len(ddoc.tables))
        b.markdown("**Settings used:** " + ", ".join(f"`{k}={v}`" for k, v in pipeline_options().items()))
        sh = qc.get("sharding")
        if sh:
            b.markdown(f"**Sharded conversion:** {len(sh['shards'])} shards of up to {sh['shard_pages']} pages on {sh['workers']} workers · "
                       f"{sh['pages_converted']}/{sh['pages']} pages · {sh['scanned_pages']} scanned · status `{sh['status']}`"
                       + (f" · failed pages {sh['failed_pages']}" if sh["failed_pages"] else ""))
            b.dataframe(pd.DataFrame([{k: s.get(k) for k in ("start", "end", "scanned", "status", "attempts", "seconds", "worker", "error")}
                                      for s in sh["shards"]]), hide_index=True, use_container_width=True)
        else:
            b.caption("Converted as one whole file (sharding is used only when workers run on separate machines; see the scaling plan).")
        labels = Counter(t.label.value for t in ddoc.texts)
        b.markdown("**Items by label:** " + ", ".join(f"`{k}` ×{v}" for k, v in labels.most_common()))
        rows = []
        for t in ddoc.texts:
            p = t.prov[0] if t.prov else None
            rows.append({"ref": t.self_ref, "label": t.label.value, "page": p.page_no if p else None,
                         "bbox (l,t,r,b)": f"{p.bbox.l:.0f},{p.bbox.t:.0f},{p.bbox.r:.0f},{p.bbox.b:.0f}" if p else "",
                         "text": t.text[:160]})
        b.markdown("**Raw Docling items (in reading order)**")
        b.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True, height=260)
        for i, tb in enumerate(ddoc.tables):
            with b.expander(f"Table {tb.self_ref} as rebuilt by TableFormer"):
                st.markdown(tb.export_to_markdown(doc=ddoc))
        with b.expander("Docling markdown export"):
            if d["markdown_path"] and Path(d["markdown_path"]).exists():
                st.markdown(Path(d["markdown_path"]).read_text())
        if is_pdf and ddoc.pages:
            pg = b.selectbox("Show Docling's layout boxes on page", sorted(ddoc.pages), key="lc-raw-page")
            boxes = []
            for t in list(ddoc.texts) + list(ddoc.tables):
                for p in t.prov or []:
                    boxes.append({"page": p.page_no, "l": p.bbox.l, "t": p.bbox.t, "r": p.bbox.r, "b": p.bbox.b,
                                  "origin": getattr(p.bbox.coord_origin, "value", ""), "label": t.label.value,
                                  "color": {"section_header": 0, "text": 1, "list_item": 3, "table": 2}.get(t.label.value, 4)})
            b.image(_page_image(d["path"], pg, boxes, ddoc.pages[pg].size.height),
                    caption="red = section_header · blue = text · orange = list_item · green = table")
        hist = q("SELECT s.run_id, s.started_at, s.output_json, (SELECT group_concat(c.name) FROM spans c WHERE c.parent_id=s.span_id) kids"
                 " FROM spans s WHERE s.name=? ORDER BY s.started_at DESC LIMIT 10", (f"ingest:{did}",))
        if hist:
            b.markdown("**Ingest history**")
            b.dataframe(pd.DataFrame([{"when": h["started_at"][:19], "run": h["run_id"], "steps": h["kids"],
                                       "result": json.dumps((jl(h["output_json"]) or {}), default=str)[:160]} for h in hist]),
                        hide_index=True, use_container_width=True)

    # 4 ------------------------------------------------------------------------------------------------
    drops = [x for x in decisions if x["step"] == "drop"]
    b = step(4, "Clean-up", "Items that are not contract content are dropped: page headers/footers, pictures (logos, signatures), "
             "confidentiality lines, signature blocks, page numbers.", "nothing stored - dropped items simply do not reach the sections")
    b.dataframe(pd.DataFrame(drops), hide_index=True, use_container_width=True) if drops else b.write("Nothing dropped.")

    # 5 ------------------------------------------------------------------------------------------------
    heads = [x for x in decisions if x["step"] in ("label", "heading", "repair", "sequence", "duplicate")]
    b = step(5, "Labels, nesting & repair", "Each heading or clause label is recognised (ARTICLE IV, Section 4.02, 4.2.1, (a), (i), "
             "Schedule A, WHEREAS...), split from run-in text (`Section 4.02 Payout. The Trader...`), and nested by a style stack: "
             "article, then decimal depth, then clause styles in the order they first appear. Sibling order is checked; schedules and "
             "exhibits get their own numbering; headings that lost their number are repaired from their neighbours.",
             f"`conversions.qc_json` (label styles {qc.get('label_styles')}, sequence issues {qc.get('sequence_issues')}, "
             f"repaired {qc.get('headings_repaired')})")
    if heads:
        kinds = Counter(x["step"] for x in heads)
        b.markdown("  ·  ".join(f"**{k}** ×{v}" for k, v in kinds.items()))
        show = b.multiselect("Show decisions", sorted(kinds), default=[k for k in kinds if k != "label"] or list(kinds), key="lc-dec")
        sel = [x for x in heads if x["step"] in show]
        if sel:
            b.dataframe(pd.DataFrame(sel).reindex(columns=["step", "ref", "label", "number", "text", "reason"]),
                        hide_index=True, use_container_width=True)
    n_fixed = sum(1 for x in heads if x["step"] == "repair" or "promoted" in x.get("reason", ""))
    b.success(f"{n_fixed} heading(s) needed fixing." if n_fixed else "All headings came through cleanly.")

    # 6 ------------------------------------------------------------------------------------------------
    b = step(6, "Section tree", "Each heading collects the paragraphs, list items and tables that follow it, until the next heading. "
             "Result: one row per clause with its parent, path, pages, text, tables and the Docling items it was built from.",
             "`sections` table (section_id, number, heading, path_json, level, parent_id, pages_json, text, tables_json, "
             "item_refs_json, bboxes_json)")
    if secs:
        b.dataframe(pd.DataFrame([{"section_id": s["section_id"], "tree": "    " * max(0, s["level"] - 1) + f"{s['number'] or ''} {s['heading']}",
                                   "parent": s["parent_id"], "pages": s["pages_json"], "docling items": len(jl(s["item_refs_json"]) or []),
                                   "tables": len(jl(s["tables_json"]) or []), "text": s["text"][:140]} for s in secs]),
                    hide_index=True, use_container_width=True)
        sel = b.selectbox("Inspect a section", [s["section_id"] for s in secs], key="lc-sec")
        s = next(x for x in secs if x["section_id"] == sel)
        cc = b.columns([1, 1])
        with cc[0]:
            st.markdown(f"**{s['number'] or ''} {s['heading']}** · path {jl(s['path_json'])}")
            st.code(s["text"] or "(no body text - article heading)", language=None)
            for t in jl(s["tables_json"]) or []:
                st.markdown(t)
            st.caption(f"Built from Docling items: {', '.join(jl(s['item_refs_json']) or [])}")
        with cc[1]:
            bxs = jl(s["bboxes_json"]) or []
            if is_pdf and bxs:
                st.image(_page_image(d["path"], bxs[0]["page"], [{**x, "color": 0} for x in bxs],
                                     (qc.get("page_sizes", {}).get(str(bxs[0]["page"])) or {}).get("h")),
                         caption=f"{sel} on page {bxs[0]['page']}")
            elif not is_pdf:
                st.caption("DOCX has no page geometry.")

    # 7 ------------------------------------------------------------------------------------------------
    b = step(7, "Quality checks", "Automatic checks before any agent uses the document. A flagged document is visible to agents but "
             "marked in the inventory they receive.", "`conversions.qc_json`")
    flags = qc.get("flags", [])
    (b.warning if flags else b.success)("Flags: " + ", ".join(flags) if flags else "No QC flags.")
    b.json({k: v for k, v in qc.items() if k != "decisions"}, expanded=False)
    b.caption("Possible flags: conversion_not_success (Docling partial/failed), no_sections (nothing structured found), "
              "unnumbered_headings (repair could not number every heading).")

    # 8 ------------------------------------------------------------------------------------------------
    b = step(8, "LLM enrichment (Digitization agent)", "The only LLM step in pre-processing. The model reads the section texts and "
             "writes navigation metadata: a summary and taxonomy tags per section, defined terms, cross-references, scope notes; and "
             "for the document a summary, terms covered, a stated effective date and a scope limit. Code guardrails then clean it. "
             "This metadata is for finding sections only - never used as evidence for values.",
             "`doc_enrichment` row; `sections.summary / term_tags_json / defined_terms_json / cross_refs_json / scope_notes_json`")
    if not enr:
        b.warning("Not enriched yet.")
    else:
        llm = q1("SELECT * FROM spans WHERE kind='llm' AND (name=? OR name LIKE ?) ORDER BY started_at DESC LIMIT 1",
                 (f"digitizer:{did}", f"digitizer:{did}:%"))
        mp = q1("SELECT output_json FROM spans WHERE name=? ORDER BY started_at DESC LIMIT 1", (f"enrich-map:{did}",))
        ar = q1("SELECT output_json FROM spans WHERE name=? ORDER BY started_at DESC LIMIT 1", (f"enrich-articles:{did}",))
        if mp:
            m, a = jl(mp["output_json"]) or {}, (jl(ar["output_json"]) if ar else None) or {}
            b.markdown(f"**Map:** {m.get('batches')} batch(es) in parallel · {m.get('enriched')} sections enriched · "
                       f"{m.get('reused')} reused from the section cache · {m.get('retried_sections')} retried · {m.get('signals')} document signals  \n"
                       f"**Reduce:** {a.get('articles', 0)} article summaries ({a.get('reused', 0)} reused) → one document-level call")
            arts = q("SELECT section_id, summary FROM article_enrichment WHERE doc_id=?", (did,))
            if arts:
                with b.expander(f"Article summaries ({len(arts)})"):
                    st.dataframe(pd.DataFrame(arts), hide_index=True, use_container_width=True)
        c = b.columns(5)
        c[0].metric("Model", enr["model"])
        c[1].metric("Prompt", enr["prompt_version"])
        c[2].metric("Tokens", f"{(llm['tokens_in'] or 0) + (llm['tokens_out'] or 0):,}" if llm else "-")
        c[3].metric("Cost", f"${llm['cost']:.4f}" if llm and llm["cost"] else "-")
        c[4].metric("Seconds", round((llm["latency_ms"] or 0) / 1000, 1) if llm else "-")
        raw = (jl(llm["output_json"]) if llm else None) or {}
        sent = (jl(llm["input_json"]) if llm else None) or {}
        b.markdown("**Document-level result (stored)**")
        b.json({"doc_summary": enr["doc_summary"], "terms_covered": jl(enr["terms_covered_json"]),
                "effective_date_in_text": enr["effective_date_in_text"], "doc_scope_note": enr["doc_scope_note"]})
        if raw:
            changes = []
            if raw.get("doc_scope_note") != enr["doc_scope_note"]:
                changes.append({"field": "doc_scope_note", "LLM said": raw.get("doc_scope_note"), "stored": enr["doc_scope_note"],
                                "why": "not a period limit (e.g. 'Performance Year 2025 only') - dropped"})
            if sorted(raw.get("terms_covered") or []) != sorted(jl(enr["terms_covered_json"]) or []):
                changes.append({"field": "terms_covered", "LLM said": raw.get("terms_covered"), "stored": jl(enr["terms_covered_json"]),
                                "why": "union with the terms of the section tags (LLM list can miss terms)"})
            stored_tags = {s["section_id"]: jl(s["term_tags_json"]) or [] for s in secs}
            for rs in raw.get("sections", []):
                if sorted(rs.get("term_tags") or []) != sorted(stored_tags.get(rs["section_id"], [])):
                    changes.append({"field": f"tags {rs['section_id']}", "LLM said": rs.get("term_tags"),
                                    "stored": stored_tags.get(rs["section_id"]), "why": "tags not in the taxonomy were removed"})
            b.markdown("**Guardrail corrections (LLM output → stored)**")
            b.dataframe(pd.DataFrame(changes), hide_index=True, use_container_width=True) if changes else b.write("None - stored as the LLM returned it.")
        b.markdown("**Per-section result (stored)**")
        b.dataframe(pd.DataFrame([{"section": s["section_id"], "heading": f"{s['number'] or ''} {s['heading']}",
                                   "✨ summary": s["summary"], "✨ tags": ", ".join(jl(s["term_tags_json"]) or []),
                                   "✨ defined terms": ", ".join(jl(s["defined_terms_json"]) or []),
                                   "✨ cross refs": ", ".join(jl(s["cross_refs_json"]) or []),
                                   "✨ scope notes": " | ".join(jl(s["scope_notes_json"]) or [])} for s in secs]),
                    hide_index=True, use_container_width=True)
        if llm:
            with b.expander("Exactly what was sent to the LLM"):
                st.markdown("**System prompt**")
                st.code(sent.get("system", ""), language="markdown")
                st.markdown("**User message (document + sections, JSON)**")
                try:
                    st.json(json.loads(sent.get("user", "{}")), expanded=1)
                except (json.JSONDecodeError, TypeError):
                    st.code(str(sent.get("user"))[:5000])
            with b.expander("Raw LLM output (before guardrails)"):
                st.json(raw, expanded=1)

    # 9 ------------------------------------------------------------------------------------------------
    from compdoc.tools.search import tokenize
    b = step(9, "Search index", "Each section is added to the BM25 keyword index (heading path + text + tables + LLM summary) and "
             "embedded with a local model for semantic search; both feed the hybrid search that builds Doc Reader shortlists.",
             "BM25 in memory; vectors in `section_vectors` (keyed by section hash + model)")
    try:
        nv = q("SELECT count(*) n FROM section_vectors v JOIN section_text t ON t.section_hash=v.section_hash WHERE t.doc_id=?", (did,))[0]["n"]
        b.caption(f"{nv} embedding chunks stored for this document.")
    except Exception:  # noqa: BLE001 - table appears after the first embedding run
        pass
    if secs:
        b.dataframe(pd.DataFrame([{"section": s["section_id"],
                                   "indexed text": (" ".join(jl(s["path_json"]) or []) + " | " + s["text"][:120] + " | " + (s["summary"] or "")[:80]),
                                   "tokens": len(tokenize(" ".join(jl(s["path_json"]) or []) + " " + s["text"] + " " +
                                                         " ".join(jl(s["tables_json"]) or []) + " " + (s["summary"] or "")))} for s in secs]),
                    hide_index=True, use_container_width=True)

    # 10 -----------------------------------------------------------------------------------------------
    b = step(10, "Ready for agents", "Pre-processing is done. This is what the agents see through their tools, and where this "
              "document has been used since.", "-")
    from compdoc.tools.docs import get_section_outline, list_trader_docs
    inv = next((x for x in list_trader_docs(d["trader_id"]) if x["doc_id"] == did), None)
    b.markdown("**`list_trader_docs` entry** (what the Orchestrator and Researchers see in their inventory)")
    b.json(inv, expanded=False)
    b.markdown("**`get_section_outline`** (what a Researcher scans before choosing sections)")
    b.dataframe(pd.DataFrame([{"section": "    " * max(0, o["level"] - 1) + f"{o['number'] or ''} {o['heading']}",
                               "tags": ", ".join(o["term_tags"]), "summary": o["summary"]} for o in get_section_outline(did)]),
                hide_index=True, use_container_width=True)
    used = q("SELECT run_id, count(*) n, group_concat(DISTINCT path) paths, max(created_at) at FROM findings"
             " WHERE json_extract(source_json,'$.doc_id')=? GROUP BY run_id ORDER BY at DESC LIMIT 10", (did,))
    b.markdown("**Used by extraction runs** (findings recorded from this document)")
    b.dataframe(pd.DataFrame(used), hide_index=True, use_container_width=True) if used else b.write("Not used yet.")
    aud = q("SELECT at, actor, action, detail_json FROM audit WHERE detail_json LIKE ? ORDER BY audit_id DESC LIMIT 20", (f'%"{did}"%',))
    if aud:
        with b.expander("Audit trail"):
            st.dataframe(pd.DataFrame(aud), hide_index=True, use_container_width=True)
