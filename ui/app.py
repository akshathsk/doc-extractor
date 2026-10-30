"""Business UI: Extract | Review | Chat | Term Finder | Drafts.

Run: uv run streamlit run ui/app.py --server.port 8501
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from compdoc import service  # noqa: E402
from compdoc.agents.chat import chat_turn  # noqa: E402
from compdoc.agents.drafter import run_drafter  # noqa: E402
from compdoc.config import settings  # noqa: E402
from compdoc.db import q  # noqa: E402
from compdoc.schemas import sub_term_paths  # noqa: E402
from compdoc.tools.access import AccessDenied, can_see  # noqa: E402
from compdoc.tools.clauses import find_docs_with_term, get_clause_variants  # noqa: E402
from compdoc.tools.extracts import compare_versions, extract_versions  # noqa: E402

st.set_page_config(page_title="Comp Doc Extractor", layout="wide")
st.sidebar.title("Comp Doc Extractor")
caller = st.sidebar.selectbox("Acting as (caller)", ["system", "analyst_a", "analyst_b", "guest"],
                              help="Mock identity used for entitlements. analyst_a sees T-1001..T-1005, analyst_b T-1006..T-1010.")
st.sidebar.caption(f"LLM provider: **{settings.provider}** · agent model `{settings.agent_model}` · fast model `{settings.fast_model}`")
st.sidebar.caption("Developer view: `streamlit run devconsole/app.py --server.port 8502`")

traders = service.traders()
trader_ids = [t["trader_id"] for t in traders]
tab_extract, tab_review, tab_chat, tab_finder, tab_drafts = st.tabs(["Extract", "Review", "Chat", "Term Finder", "Drafts"])

STATUS_ICON = {"current": "🟢", "removed": "⚪", "conflict": "🔴", "needs_review": "🟠"}


def render_extract(ex) -> None:
    c1, c2, c3 = st.columns(3)
    c1.metric("Version", f"v{ex.version}")
    c2.metric("Leaves", len(ex.flat()))
    c3.metric("Open conflicts", sum(1 for c in ex.conflicts if not c.resolution or c.resolution.get("action") == "escalate"))
    if ex.summary:
        st.info(ex.summary)
    for t in ex.terms:
        with st.expander(f"**{t.term}**", expanded=True):
            for s in t.sub_terms:
                st.markdown(f"**{s.sub_term}**")
                rows = []
                for lf in s.leaves:
                    rows.append({"": STATUS_ICON.get(lf.status, ""), "key": lf.key, "value": "" if lf.value is None else str(lf.value), "unit": lf.unit, "status": lf.status,
                                 "source": f"{lf.source.doc_id} · {lf.source.effective_date} · §{lf.source.section_id.split(':s')[-1]}"
                                           f"{' · p' + str(lf.source.page) if lf.source.page else ''}",
                                 "history": " ← ".join(f"{h.value} ({h.doc_id} {h.effective_date})" for h in lf.history),
                                 "quote": lf.source.quote, "decision": lf.decision})
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    with st.expander("Documents"):
        st.dataframe(pd.DataFrame(ex.documents), hide_index=True, use_container_width=True)


with tab_extract:
    col_a, col_b = st.columns([1, 3])
    with col_a:
        tid = st.selectbox("Trader", trader_ids, format_func=lambda t: f"{t} · {next(x['trader_name'] for x in traders if x['trader_id'] == t)}")
        if not can_see(caller, tid):
            st.error(f"{caller} is not entitled to {tid}")
        elif st.button("Run extraction", type="primary"):
            with st.spinner("Researchers are reading the documents…"):
                res = service.extract_trader(tid, caller)
            st.session_state["last_run"] = res
            if res["status"] == "pending_review":
                st.warning(f"Run {res['run_id']} paused: {len(res['review']['items'])} item(s) need review (Review tab).")
            else:
                st.success(f"Published v{res['version']} (run {res['run_id']})")
        vs = extract_versions(tid)
        if len(vs) >= 2 and can_see(caller, tid):
            a, b = st.select_slider("Compare versions", options=[v["version"] for v in vs], value=(vs[-2]["version"], vs[-1]["version"]))
            if a != b:
                st.json(compare_versions(tid, a, b, caller)["changes"])
    with col_b:
        if can_see(caller, tid):
            ex = service.get_extract(tid, caller)
            render_extract(ex) if ex else st.write("No published extract yet - run extraction.")

with tab_review:
    pend = [r for r in service.pending_reviews() if can_see(caller, r["trader_id"])]
    if not pend:
        st.success("Nothing waiting for review.")
    import json as _json
    for r in pend:
        st.subheader(f"{r['trader_id']} · run {r['run_id']}")
        items = _json.loads(r["payload_json"])
        decisions = {}
        for it in items:
            st.markdown(f"**{it['path']}** — `{it['kind']}`  \n{it['note']}")
            opts = {f"finding {c['finding_id']}: {c['value']} ({c['doc_id']}, {c['effective_date']}){' · scope: ' + c['scope_note'] if c['scope_note'] else ''}": c
                    for c in it["candidates"]}
            for label, c in opts.items():
                st.caption(f"{label}\n> {c['quote']}")
            choice = st.radio("Decision", list(opts) + ["manual value"], key=f"{r['run_id']}-{it['path']}", horizontal=False)
            if choice == "manual value":
                decisions[it["path"]] = {"action": "manual", "value": st.text_input("Value", key=f"v-{r['run_id']}-{it['path']}"),
                                         "reason": "manual entry"}
            else:
                decisions[it["path"]] = {"action": "pick", "finding_id": opts[choice]["finding_id"], "reason": "reviewer pick"}
        reviewer = st.text_input("Reviewer name", value=caller, key=f"rev-{r['run_id']}")
        if st.button("Submit decisions & publish", key=f"btn-{r['run_id']}", type="primary"):
            out = service.resume_review(r["run_id"], decisions, reviewer)
            st.success(f"{out['status']} v{out.get('version')}")
            st.rerun()

with tab_chat:
    st.caption("Ask about a trader (\"What is T-1003's payout and when did it change?\"), find terms across documents "
               "(\"Which docs have a clawback clause?\") or request a draft (\"Draft an amendment for T-1003 raising payout to 18%\").")
    hist = st.session_state.setdefault("chat", [])

    def page_snippet(c: dict):
        """Crop of the PDF page around the quoted Docling item, outlined in red."""
        doc = q("SELECT path FROM documents WHERE doc_id=?", (c["doc_id"],))
        b = c.get("bbox")
        if not doc or not b or not doc[0]["path"].endswith(".pdf") or b.get("l") is None:
            return None
        import pypdfium2 as pdfium
        from PIL import ImageDraw
        pdf = pdfium.PdfDocument(doc[0]["path"])
        page = pdf[(b.get("page") or c["page"] or 1) - 1]
        scale, H = 2.0, page.get_height()
        img = page.render(scale=scale).to_pil().convert("RGB")
        pdf.close()
        top, bot = ((H - b["t"]) * scale, (H - b["b"]) * scale) if "BOTTOM" in str(b.get("origin", "")).upper() else (b["t"] * scale, b["b"] * scale)
        top, bot = min(top, bot), max(top, bot)
        ImageDraw.Draw(img).rectangle([b["l"] * scale - 4, top - 4, b["r"] * scale + 4, bot + 4], outline=(220, 30, 30), width=4)
        return img.crop((0, max(0, top - 120), img.width, min(img.height, bot + 120)))

    def render_citations(cites: list[dict], key: str) -> None:
        if not cites:
            return
        with st.expander(f"📎 Citations ({len(cites)})", expanded=True):
            for c in cites:
                loc = f"p.{c['page']}" if c.get("page") else "DOCX (no page geometry)"
                st.markdown(f"**[{c['n']}] {c['doc_id']}** · {c['doc_title']} · {c['doc_date']} · {c['trader_id']} · "
                            f"§{c['section']} · {loc}")
                if c.get("quote"):
                    st.markdown(f"> {c['quote']}")
                st.caption(f"Docling item `{c.get('docling_ref') or '-'}` · section `{c['section_id']}` · found via {c['via']}")
                if c.get("bbox") and st.checkbox("Show on page", key=f"{key}-{c['n']}"):
                    img = page_snippet(c)
                    if img is not None:
                        st.image(img, caption=f"{c['doc_id']} page {c['page']} · Docling bbox")

    for i, m in enumerate(hist):
        with st.chat_message(m["role"]):
            st.markdown(m["content"], unsafe_allow_html=True)
            render_citations(m.get("citations") or [], f"h{i}")
    if msg := st.chat_input("Ask…"):
        hist.append({"role": "user", "content": msg})
        with st.chat_message("user"):
            st.markdown(msg)
        cites: list[dict] = []
        with st.chat_message("assistant"), st.spinner("Thinking…"):
            try:
                out = chat_turn(msg, caller, [{"role": h["role"], "content": h["content"]} for h in hist[:-1]][-8:])
                answer = f"{out['answer']}\n\n<sub>route: {out['route']} · run {out['run_id']}</sub>"
                cites = out.get("citations") or []
            except AccessDenied as e:
                answer = f"Access denied: {e}"
            st.markdown(answer, unsafe_allow_html=True)
            render_citations(cites, f"h{len(hist)}")
        hist.append({"role": "assistant", "content": answer, "citations": cites})

with tab_finder:
    paths = sorted({p.split(".")[0] for p in sub_term_paths()} | set(sub_term_paths()))
    tp = st.selectbox("Term", paths, index=paths.index("Clawback") if "Clawback" in paths else 0)
    res = find_docs_with_term(tp, caller=caller)
    st.write(f"**{res['count']}** sections in **{res['documents']}** documents (newest first)"
             + (f" · text withheld for {res['withheld_text_for']} (not entitled)" if res["withheld_text_for"] else ""))
    st.dataframe(pd.DataFrame(res["results"]).drop(columns=["entitled"], errors="ignore"), hide_index=True, use_container_width=True)
    st.markdown("**Wording variants**")
    for v in get_clause_variants(tp, caller=caller)["variants"]:
        st.markdown(f"`{v['variant_id']}` · {v['doc_count']} docs · latest {v['latest_doc_date']} ({v['latest_doc_id']})")
        st.caption(v["canonical_text"] or "[withheld]")

with tab_drafts:
    tid_d = st.selectbox("Trader for draft", trader_ids, key="draft_trader")
    req = st.text_area("Request", "Draft an amendment raising payout to 18% and moving to 4 year vesting")
    if st.button("Draft from precedent", type="primary"):
        with st.spinner("Drafting…"):
            try:
                out = run_drafter(tid_d, req, caller)
                st.success(out["answer"])
            except AccessDenied as e:
                st.error(str(e))
    for d in q("SELECT draft_id, trader_id, request, path, status, created_at FROM drafts ORDER BY created_at DESC LIMIT 20"):
        if not can_see(caller, d["trader_id"]):
            continue
        c1, c2 = st.columns([4, 1])
        c1.markdown(f"**{d['draft_id']}** · {d['trader_id']} · {d['status']} · {d['created_at'][:16]}  \n{d['request']}")
        if Path(d["path"]).exists():
            c2.download_button("Download .docx", Path(d["path"]).read_bytes(), file_name=Path(d["path"]).name, key=d["draft_id"])
