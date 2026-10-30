"""Developer Console: how every result was produced, and what was stored.

Pages: Runs · Run detail · State inspector · Data explorer · Playground · Prompts & evals
Run: uv run streamlit run devconsole/app.py --server.port 8502
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from compdoc.config import settings  # noqa: E402
from compdoc.db import q, q1  # noqa: E402

st.set_page_config(page_title="compdoc · Dev Console", layout="wide")
PAGES = ["Ask & trace", "Document lifecycle", "Runs", "Run detail", "State inspector", "Data explorer", "Playground", "Prompts & evals"]
page = st.sidebar.radio("Dev Console", PAGES, index=PAGES.index(st.session_state.get("page", "Ask & trace")))
st.session_state["page"] = page
st.sidebar.caption(f"DB: `{settings.db_path}`  \nprovider **{settings.provider}** · `{settings.agent_model}` / `{settings.fast_model}`")


def jl(s: str | None):
    try:
        return json.loads(s) if s else None
    except json.JSONDecodeError:
        return s


def pick_run(default_type: str | None = None) -> str | None:
    runs = q("SELECT run_id, type, trader_id, status, started_at FROM runs ORDER BY started_at DESC LIMIT 300")
    if not runs:
        st.info("No runs yet.")
        return None
    ids = [r["run_id"] for r in runs]
    cur = st.session_state.get("run_id")
    idx = ids.index(cur) if cur in ids else 0
    rid = st.sidebar.selectbox("Run", ids, index=idx,
                               format_func=lambda r: next(f"{x['run_id']} · {x['trader_id'] or ''} · {x['status']}" for x in runs if x["run_id"] == r))
    st.session_state["run_id"] = rid
    return rid


ICON = {"node": "🟦", "agent": "🤖", "llm": "💬", "tool": "🔧"}


def _short(v, n: int = 160) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "…"


def _summarize_output(out) -> str:
    if out is None:
        return "no output"
    if isinstance(out, dict) and "error" in out:
        return f"⚠️ error: {_short(out['error'])}"
    if isinstance(out, list):
        first = out[0] if out else None
        hint = ""
        if isinstance(first, dict):
            hint = " · first: " + _short({k: first[k] for k in list(first)[:5]}, 140)
        return f"{len(out)} result(s){hint}"
    if isinstance(out, dict):
        for k in ("results", "variants", "leaves", "terms"):
            if k in out:
                return f"{len(out[k])} {k}" + (f" · count={out['count']}" if "count" in out else "")
        if "recorded" in out:
            return f"recorded={out['recorded']} quote_verified={out.get('quote_verified')}" + (f" · {out.get('error')}" if out.get("error") else "")
        if "text" in out and "section_id" in out:
            return f"§{out.get('number') or ''} {out.get('heading')}: {_short(out['text'], 140)}"
        return _short(out)
    return _short(out)


def describe_step(s: dict) -> str:
    """One-line, human-readable decision/action for a span."""
    k, n = s["kind"], s["name"]
    inp, out = jl(s["input_json"]) or {}, jl(s["output_json"])
    if k == "llm" and n == "chat_router" and isinstance(out, dict):
        return (f"**Router decided → `{out.get('route')}`** · trader `{out.get('trader_id')}` · term `{out.get('term_path')}`")
    if k == "llm" and (":turn" in n or n.endswith(":final")) and isinstance(out, dict):
        calls = out.get("function_calls") or out.get("tool_calls") or []
        if calls:
            return "**Model decided to call** " + ", ".join(f"`{c['name']}({_short(c.get('arguments', ''), 110)})`" for c in calls)
        return f"**Model decided it had enough and wrote the answer:** {_short(out.get('text') or out.get('content') or '', 300)}"
    if k == "llm":
        return f"LLM output: {_short(out, 300)}"
    if k == "agent":
        if isinstance(out, dict) and "final" in out:
            return f"Agent finished after {out.get('steps', out.get('tool_calls'))} step(s), {out.get('tool_calls')} tool call(s)"
        return f"Agent started with tools {inp.get('tools')}"
    if k == "tool":
        if n == "build_citations":
            return f"Built **{len(out or [])} citation(s)** from the sources the agent read: " + \
                ", ".join(f"{c['doc_id']} §{c['section']}" for c in (out or []))
        args = {kk: vv for kk, vv in inp.items() if kk != "args"} if isinstance(inp, dict) else inp
        return f"`{n}({_short(args, 140)})` → {_summarize_output(out)}"
    return _short(out, 200)


def ordered_steps(run_id: str) -> list[tuple[int, dict]]:
    """Spans in the order they started, with tree depth."""
    spans = q("SELECT * FROM spans WHERE run_id=? ORDER BY started_at, seq", (run_id,))
    by_id = {s["span_id"]: s for s in spans}

    def depth(s: dict) -> int:
        d, p = 0, s["parent_id"]
        while p and p in by_id:
            d, p = d + 1, by_id[p]["parent_id"]
        return d
    return [(depth(s), s) for s in spans]


def render_trace(run_id: str, live: bool = False) -> None:
    steps = ordered_steps(run_id)
    if not steps:
        st.caption("waiting for the first step…")
        return
    t0 = pd.to_datetime(steps[0][1]["started_at"])
    for i, (d, s) in enumerate(steps, 1):
        t = (pd.to_datetime(s["started_at"]) - t0).total_seconds()
        tok = (s["tokens_in"] or 0) + (s["tokens_out"] or 0)
        head = (f"{'&nbsp;' * 6 * d}{'↳ ' if d else ''}**{i}.** {ICON.get(s['kind'], '')} `{s['name']}` "
                f"<span style='color:gray'>+{t:.1f}s · {s['latency_ms']:.0f} ms{f' · {tok:,} tok' if tok else ''}"
                f"{' · ' + s['model'] if s['model'] else ''}</span>")
        st.markdown(head, unsafe_allow_html=True)
        st.markdown(f"{'&nbsp;' * 6 * (d + 1)}{'❌ ' + s['error'] if s['error'] else describe_step(s)}", unsafe_allow_html=True)
        if not live:
            with st.expander(f"Step {i} details · input / output", expanded=False):
                c1, c2 = st.columns(2)
                c1.markdown("**Input**")
                c1.json(jl(s["input_json"]), expanded=1)
                c2.markdown("**Output**")
                c2.json(jl(s["output_json"]), expanded=1)


# ===================================================================================== Ask & trace
if page == "Ask & trace":
    import threading
    import time
    import uuid

    from compdoc.agents.chat import chat_turn
    st.title("Ask & trace")
    st.caption("Ask a question and watch every step the system takes - routing, each model decision, every tool call and its "
               "result, citation building - in the order it happened. Model reasoning itself is not exposed by the API; "
               "you see each decision it made (which tool, with which arguments, or to answer).")
    c1, c2 = st.columns([4, 1])
    question = c1.text_input("Question", placeholder="e.g. When did T-1003's deferral change, and what is it now?")
    caller = c2.selectbox("Caller", ["system", "analyst_a", "analyst_b", "guest"])
    if st.button("Ask", type="primary", disabled=not question):
        run_id = f"chat-{uuid.uuid4().hex[:10]}"
        result: dict = {}

        def work():
            try:
                result["out"] = chat_turn(question, caller, run_id=run_id)
            except Exception as e:  # noqa: BLE001
                result["error"] = f"{type(e).__name__}: {e}"
        th = threading.Thread(target=work, daemon=True)
        th.start()
        live = st.empty()
        while th.is_alive():
            with live.container():
                st.info(f"Running {run_id}… steps appear as they complete")
                render_trace(run_id, live=True)
            time.sleep(0.8)
        th.join()
        live.empty()
        st.session_state["trace_run"] = run_id
        st.session_state["trace_result"] = result

    chat_runs = q("SELECT run_id, input_json, started_at FROM runs WHERE type='chat' ORDER BY started_at DESC LIMIT 100")
    if chat_runs:
        ids = [r["run_id"] for r in chat_runs]
        cur = st.session_state.get("trace_run")
        rid = st.selectbox("Trace of", ids, index=ids.index(cur) if cur in ids else 0,
                           format_func=lambda r: next(f"{x['started_at'][:19]} · {(jl(x['input_json']) or {}).get('message', '')[:90]} · {x['run_id']}"
                                                      for x in chat_runs if x["run_id"] == r))
        run = q1("SELECT * FROM runs WHERE run_id=?", (rid,))
        out = jl(run["output_json"]) or {}
        m = st.columns(5)
        m[0].metric("Route", out.get("route", "-"))
        m[1].metric("Status", run["status"])
        m[2].metric("Seconds", round((run["latency_ms"] or 0) / 1000, 1))
        m[3].metric("Tokens", f"{(run['tokens_in'] or 0) + (run['tokens_out'] or 0):,}")
        m[4].metric("Cost", f"${run['cost'] or 0:.4f}")
        st.markdown(f"**Question:** {(jl(run['input_json']) or {}).get('message', '')}")
        if run["error"] or (st.session_state.get("trace_result") or {}).get("error"):
            st.error(run["error"] or st.session_state["trace_result"]["error"])
        from story import extract_run_behind, render_story
        st.subheader("Final answer")
        st.markdown(out.get("answer", "_(no answer recorded)_"))
        cites = out.get("citations") or []
        if cites:
            st.markdown("**Citations**")
            st.dataframe(pd.DataFrame([{"#": c["n"], "doc": c["doc_id"], "title": c["doc_title"], "date": c["doc_date"],
                                        "section": c["section"], "page": c.get("page"), "docling item": c.get("docling_ref"),
                                        "quote": c.get("quote")} for c in cites]), hide_index=True, use_container_width=True)
        st.header("A · What each agent did for this question")
        st.caption("One block per agent, in order. Each step is expandable: model decisions show the tool call it chose; "
                   "tool steps show the response (tables, section text, outlines). The table under each agent lists the "
                   "sections it found, read, used and cited.")
        render_story(rid, key="chat")
        behind = extract_run_behind(rid)
        if behind:
            st.header("B · How these values were extracted")
            st.caption("The answer used the published extract. These are the agents of the extraction run that built it - "
                       "this is where documents were searched, outlines read and sections chosen.")
            for tid, xrun, ver in behind:
                if st.toggle(f"Show extraction run {xrun} ({tid}, extract v{ver})", key=f"x-{xrun}", value=True):
                    render_story(xrun, key=xrun)
        with st.expander("Raw step list (all spans in order)"):
            render_trace(rid)
        if st.button("Open in Run detail →"):
            st.session_state.update(run_id=rid, page="Run detail")
            st.rerun()
    else:
        st.info("No chat runs yet - ask a question above.")

# ===================================================================================== Document lifecycle
elif page == "Document lifecycle":
    from lifecycle import render_lifecycle
    render_lifecycle()

# ===================================================================================== Runs
elif page == "Runs":
    st.title("Runs")
    c1, c2, c3 = st.columns(3)
    rtype = c1.multiselect("Type", ["ingest", "extract", "chat", "draft", "playground"], default=[])
    trader = c2.text_input("Trader contains")
    status = c3.multiselect("Status", ["ok", "error", "interrupted", "running"], default=[])
    runs = pd.DataFrame(q("SELECT run_id, type, trader_id, caller, status, started_at, round(latency_ms/1000.0,1) AS secs,"
                          " tokens_in, tokens_out, round(cost,4) AS cost_usd, error,"
                          " (SELECT count(*) FROM spans s WHERE s.run_id=r.run_id AND kind='tool') AS tool_calls,"
                          " (SELECT count(*) FROM spans s WHERE s.run_id=r.run_id AND kind='llm') AS llm_calls"
                          " FROM runs r ORDER BY started_at DESC LIMIT 500"))
    if not runs.empty:
        if rtype:
            runs = runs[runs.type.isin(rtype)]
        if trader:
            runs = runs[runs.trader_id.fillna("").str.contains(trader)]
        if status:
            runs = runs[runs.status.isin(status)]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Runs", len(runs))
        m2.metric("Errors", int((runs.status == "error").sum()))
        m3.metric("Tokens", f"{int(runs.tokens_in.sum() + runs.tokens_out.sum()):,}")
        m4.metric("Cost", f"${runs.cost_usd.sum():.3f}")
        st.dataframe(runs, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row", key="runs_tbl")
        sel = st.session_state.get("runs_tbl", {}).get("selection", {}).get("rows", [])
        if sel:
            st.session_state["run_id"] = runs.iloc[sel[0]].run_id
            if st.button(f"Open {st.session_state['run_id']} →"):
                st.session_state["page"] = "Run detail"
                st.rerun()

# ===================================================================================== Run detail
elif page == "Run detail":
    rid = pick_run()
    if rid:
        run = q1("SELECT * FROM runs WHERE run_id=?", (rid,))
        st.title(f"Run {rid}")
        c = st.columns(6)
        c[0].metric("Type", run["type"])
        c[1].metric("Status", run["status"])
        c[2].metric("Seconds", round((run["latency_ms"] or 0) / 1000, 1))
        c[3].metric("Tokens in/out", f"{run['tokens_in']:,}/{run['tokens_out']:,}")
        c[4].metric("Cost", f"${run['cost'] or 0:.4f}")
        c[5].metric("Trader", run["trader_id"] or "-")
        if run["error"]:
            st.error(run["error"])
        spans = q("SELECT * FROM spans WHERE run_id=? ORDER BY started_at, seq", (rid,))

        if run["type"] == "extract":
            from compdoc.graph import build_graph
            g = build_graph().compile().get_graph()
            ran = {s["name"] for s in spans if s["kind"] == "node"}
            researchers = [s for s in spans if s["kind"] == "agent" and s["name"].startswith("researcher:")]
            dot = ["digraph G { rankdir=LR; node [shape=box, style=\"rounded,filled\", fontname=Helvetica, fontsize=11];"]
            for n in g.nodes:
                if n in ("__start__", "__end__"):
                    dot.append(f'"{n}" [shape=circle, label="", width=0.25, fillcolor="#999999"];')
                    continue
                done = n in ran or any(x.startswith(n) for x in ran)
                label = n if n != "researcher" else f"researcher ×{len(researchers)}\\n" + "\\n".join(s["name"].split(":")[1] for s in researchers)
                dot.append(f'"{n}" [label="{label}", fillcolor="{"#c8e6c9" if done else "#eeeeee"}"];')
            for e in g.edges:
                dot.append(f'"{e.source}" -> "{e.target}"{" [style=dashed]" if e.conditional else ""};')
            dot.append("}")
            st.graphviz_chart("\n".join(dot))

        if spans:
            df = pd.DataFrame(spans)
            df["start"] = pd.to_datetime(df.started_at)
            df["end"] = pd.to_datetime(df.ended_at)
            st.subheader("Timeline")
            chart = alt.Chart(df[df.kind.isin(["node", "agent", "llm"])]).mark_bar().encode(
                x="start:T", x2="end:T", y=alt.Y("name:N", sort=None, title=None), color="kind:N",
                tooltip=["name", "kind", "latency_ms", "tokens_in", "tokens_out", "model"]).properties(height=max(200, 14 * len(df[df.kind.isin(['node', 'agent', 'llm'])])))
            st.altair_chart(chart, use_container_width=True)

            st.subheader("Span tree")
            children: dict = {}
            for s in spans:
                children.setdefault(s["parent_id"], []).append(s)
            ordered = []

            def walk(pid, depth):
                for s in children.get(pid, []):
                    ordered.append((depth, s))
                    walk(s["span_id"], depth + 1)
            walk(None, 0)
            icon = {"node": "🟦", "agent": "🤖", "llm": "💬", "tool": "🔧"}
            kinds = st.multiselect("Show kinds", ["node", "agent", "llm", "tool"], default=["node", "agent", "llm", "tool"])
            filt = st.text_input("Filter by name (e.g. record_finding, researcher:Deferral)")
            rows = [{"tree": " " * d + f"{icon.get(s['kind'], '')} {s['name']}", "kind": s["kind"], "ms": s["latency_ms"],
                     "tokens": (s["tokens_in"] or 0) + (s["tokens_out"] or 0), "model": s["model"], "status": s["status"],
                     "span_id": s["span_id"]} for d, s in ordered if s["kind"] in kinds and (not filt or filt in s["name"])]
            tbl = pd.DataFrame(rows)
            st.dataframe(tbl, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row", key="span_tbl",
                         column_config={"span_id": None})
            sel = st.session_state.get("span_tbl", {}).get("selection", {}).get("rows", [])
            if sel:
                s = next(x for x in spans if x["span_id"] == tbl.iloc[sel[0]].span_id)
                st.markdown(f"#### {s['kind']} · `{s['name']}`  ({s['latency_ms']} ms, prompt {s['prompt_version'] or '-'})")
                if s["error"]:
                    st.error(s["error"])
                c1, c2 = st.columns(2)
                c1.markdown("**Input**")
                c1.json(jl(s["input_json"]), expanded=2)
                c2.markdown("**Output**")
                c2.json(jl(s["output_json"]), expanded=2)
        out = jl(run["output_json"])
        if out:
            st.subheader("Run output")
            st.json(out, expanded=1)

# ===================================================================================== State inspector
elif page == "State inspector":
    rid = pick_run()
    if rid and (q1("SELECT type FROM runs WHERE run_id=?", (rid,)) or {}).get("type") == "extract":
        from compdoc.graph import compiled
        from compdoc.service import graph_state_history
        st.title(f"Checkpoints · {rid}")
        hist = graph_state_history(rid)
        if not hist:
            st.info("No checkpoints for this run.")
        else:
            labels = [f"step {h['step']} · next={h['next']} · {h['source']} · {h['checkpoint_id'][-8:]}" for h in hist]
            i = st.select_slider("Checkpoint (newest → oldest)", options=list(range(len(hist))), format_func=lambda k: labels[k])
            cur, prev = hist[i], (hist[i + 1] if i + 1 < len(hist) else None)
            c1, c2 = st.columns(2)
            c1.markdown("**State at this checkpoint**")
            vals = dict(cur["values"])
            c1.json({k: v for k, v in vals.items() if k != "inventory"}, expanded=1)
            c2.markdown("**Changed vs previous checkpoint**")
            if prev:
                diff = {k: {"before": prev["values"].get(k), "after": v} for k, v in vals.items() if prev["values"].get(k) != v}
                c2.json(diff, expanded=1)
            else:
                c2.write("(first checkpoint)")
            st.divider()
            st.markdown("**Fork from this checkpoint** - edit `resolutions` and re-run the remaining nodes on a new branch "
                        "of the same thread (useful to test reviewer decisions or reconcile changes).")
            res_txt = st.text_area("resolutions JSON", json.dumps(vals.get("resolutions") or {}, indent=2), height=150)
            if st.button("Fork & continue"):
                from compdoc.observability import start_run
                cfg = {"configurable": {"thread_id": rid, "checkpoint_id": cur["checkpoint_id"]}}
                g = compiled()
                new_cfg = g.update_state(cfg, {"resolutions": json.loads(res_txt)})
                with start_run("extract", run_id=rid):
                    out = g.invoke(None, new_cfg)
                st.success("Forked run finished" if "__interrupt__" not in out else "Forked run paused for review")
                st.json({k: v for k, v in out.items() if k in ("published_version", "resolutions")})
    elif rid:
        st.info("Pick an extract run - only the extraction graph is checkpointed.")

# ===================================================================================== Data explorer
elif page == "Data explorer":
    st.title("Data explorer")
    sub = st.radio("View", ["Documents", "Evidence ledger", "Reconcile decisions", "Extract versions", "Reviews & audit", "Drafts", "SQL"],
                   horizontal=True)
    if sub == "Documents":
        docs = q("SELECT d.*, c.qc_json, c.docling_json_path, c.markdown_path, e.doc_summary, e.terms_covered_json, e.effective_date_in_text,"
                 " e.doc_scope_note, e.model, e.prompt_version FROM documents d LEFT JOIN conversions c ON c.file_hash=d.file_hash"
                 " LEFT JOIN doc_enrichment e USING(doc_id) ORDER BY d.trader_id, d.doc_date")
        dids = [d["doc_id"] for d in docs]
        did = st.selectbox("Document", dids, format_func=lambda x: next(f"{d['doc_id']} · {d['trader_id']} · {d['doc_date']} · {d['title']} ({d['format']})" for d in docs if d["doc_id"] == x))
        d = next(x for x in docs if x["doc_id"] == did)
        qc = jl(d["qc_json"]) or {}
        st.markdown(f"**1 · Source DB row** → **2 · Docling** ({qc.get('status')}, {qc.get('seconds')}s, OCR={qc.get('ocr_used')}) → "
                    f"**3 · {qc.get('sections')} sections** (headings repaired: {qc.get('headings_repaired')}) → **4 · enrichment** ({d['model']}, {d['prompt_version']})")
        if qc.get("flags"):
            st.warning(f"QC flags: {qc['flags']}")
        c1, c2 = st.columns([1, 1])
        with c1:
            st.json({k: d[k] for k in ("doc_id", "trader_id", "trader_name", "title", "doc_type", "doc_date", "format", "file_hash", "path")}, expanded=False)
            st.markdown(f"**Doc summary**: {d['doc_summary']}  \n**Terms covered**: {jl(d['terms_covered_json'])}  \n"
                        f"**Effective date in text**: {d['effective_date_in_text']}  \n**Scope note**: {d['doc_scope_note']}")
            st.json(qc, expanded=False)
        secs = q("SELECT section_id, number, heading, level, parent_id, pages_json, text, tables_json, summary, term_tags_json, defined_terms_json,"
                 " cross_refs_json, scope_notes_json, bboxes_json, item_refs_json FROM sections WHERE doc_id=? ORDER BY ord", (did,))
        sdf = pd.DataFrame([{"section": s["section_id"].split(":")[1], "number": s["number"], "heading": s["heading"], "level": s["level"],
                             "pages": s["pages_json"], "tags": ", ".join(jl(s["term_tags_json"]) or []), "summary": s["summary"],
                             "text": s["text"][:200]} for s in secs])
        with c2:
            sel_sec = st.selectbox("Highlight section", [s["section_id"] for s in secs])
        llm_by = f"{d['model']} · prompt {d['prompt_version']}" if d["model"] else "not enriched yet"
        llm_tag = "offline rules" if d["model"] == "offline" else "LLM"
        st.caption(f"✨ = generated by the Digitization Agent ({llm_by}); used for navigation only, never as evidence. "
                   "Other columns come from Docling + the section builder.")
        st.dataframe(sdf, hide_index=True, use_container_width=True, column_config={
            "summary": st.column_config.TextColumn(f"✨ summary ({llm_tag})", help=f"Generated by {llm_by}"),
            "tags": st.column_config.TextColumn(f"✨ tags ({llm_tag})", help=f"Taxonomy term tags generated by {llm_by}"),
        })
        s = next(x for x in secs if x["section_id"] == sel_sec)
        with c2:
            if d["path"].endswith(".pdf") and Path(d["path"]).exists():
                import pypdfium2 as pdfium
                from PIL import ImageDraw
                boxes = jl(s["bboxes_json"]) or []
                page_no = (boxes[0]["page"] if boxes else 1)
                pdf = pdfium.PdfDocument(d["path"])
                scale = 1.6
                img = pdf[page_no - 1].render(scale=scale).to_pil().convert("RGB")
                H = (qc.get("page_sizes", {}).get(str(page_no)) or {}).get("h") or pdf[page_no - 1].get_height()
                drw = ImageDraw.Draw(img)
                for b in boxes:
                    if b["page"] != page_no:
                        continue
                    if "BOTTOM" in str(b.get("origin", "")).upper():
                        top, bot = (H - b["t"]) * scale, (H - b["b"]) * scale
                    else:
                        top, bot = b["t"] * scale, b["b"] * scale
                    drw.rectangle([b["l"] * scale, min(top, bot), b["r"] * scale, max(top, bot)], outline=(220, 30, 30), width=3)
                st.image(img, caption=f"page {page_no} · Docling bounding boxes for {sel_sec}")
            else:
                st.caption("DOCX: Docling reads Word structure directly - no page geometry.")
            st.markdown(f"**{s['number'] or ''} {s['heading']}**")
            st.write(s["text"])
            for t in jl(s["tables_json"]) or []:
                st.markdown(t)
            st.json({"term_tags": jl(s["term_tags_json"]), "defined_terms": jl(s["defined_terms_json"]), "cross_refs": jl(s["cross_refs_json"]),
                     "scope_notes": jl(s["scope_notes_json"]), "docling_items": jl(s["item_refs_json"])}, expanded=False)
        with st.expander("Docling markdown export"):
            if d["markdown_path"] and Path(d["markdown_path"]).exists():
                st.markdown(Path(d["markdown_path"]).read_text())
        with st.expander("Raw DoclingDocument JSON (first 200 text items)"):
            if d["docling_json_path"] and Path(d["docling_json_path"]).exists():
                raw = json.loads(Path(d["docling_json_path"]).read_text())
                st.json({"texts": raw.get("texts", [])[:200], "tables": raw.get("tables", [])}, expanded=False)

    elif sub == "Evidence ledger":
        runs = q("SELECT DISTINCT run_id, trader_id FROM findings ORDER BY finding_id DESC")
        if runs:
            r = st.selectbox("Run", [x["run_id"] for x in runs], format_func=lambda x: f"{x} · {next(y['trader_id'] for y in runs if y['run_id'] == x)}")
            rows = q("SELECT finding_id, agent, path, value_json, unit, assertion, scope_note, quote_verified, source_json, created_at FROM findings WHERE run_id=? ORDER BY path, finding_id", (r,))
            st.dataframe(pd.DataFrame([{**{k: v for k, v in x.items() if k != "source_json"}, **{f"src_{k}": v for k, v in (jl(x["source_json"]) or {}).items()}} for x in rows]),
                         hide_index=True, use_container_width=True)

    elif sub == "Reconcile decisions":
        from compdoc.tools.extracts import latest_extract
        tids = [t["trader_id"] for t in q("SELECT DISTINCT trader_id FROM extracts ORDER BY trader_id")]
        if tids:
            tid = st.selectbox("Trader", tids)
            ex = latest_extract(tid)
            rows = []
            for p, lf in ex.flat().items():
                rows.append({"path": p, "value": "" if lf.value is None else str(lf.value), "status": lf.status, "winner": f"{lf.source.doc_id} {lf.source.effective_date}",
                             "decision": lf.decision, "history": " | ".join(f"{h.value} {h.doc_id} {h.effective_date}" for h in lf.history),
                             "conflict": json.dumps(lf.conflict) if lf.conflict else ""})
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            st.markdown("**Conflicts in this version**")
            st.json([c.model_dump() for c in ex.conflicts], expanded=False)

    elif sub == "Extract versions":
        from compdoc.tools.extracts import compare_versions, extract_versions
        tids = [t["trader_id"] for t in q("SELECT DISTINCT trader_id FROM extracts ORDER BY trader_id")]
        if tids:
            tid = st.selectbox("Trader", tids)
            vs = extract_versions(tid)
            st.dataframe(pd.DataFrame(vs), hide_index=True)
            if len(vs) > 1:
                a = st.selectbox("From", [v["version"] for v in vs], index=len(vs) - 2)
                b = st.selectbox("To", [v["version"] for v in vs], index=len(vs) - 1)
                st.json(compare_versions(tid, a, b))

    elif sub == "Reviews & audit":
        st.markdown("**Reviews**")
        st.dataframe(pd.DataFrame(q("SELECT * FROM reviews ORDER BY review_id DESC")), hide_index=True, use_container_width=True)
        st.markdown("**Audit log**")
        st.dataframe(pd.DataFrame(q("SELECT * FROM audit ORDER BY audit_id DESC LIMIT 500")), hide_index=True, use_container_width=True)

    elif sub == "Drafts":
        for d in q("SELECT * FROM drafts ORDER BY created_at DESC"):
            with st.expander(f"{d['draft_id']} · {d['trader_id']} · {d['status']} · {d['created_at'][:16]}"):
                st.write(d["request"])
                st.json(jl(d["draft_json"]), expanded=1)

    else:
        sql = st.text_area("Read-only SQL", "SELECT name FROM sqlite_master WHERE type='table'")
        if st.button("Run"):
            if not sql.strip().lower().startswith(("select", "with", "pragma")):
                st.error("Read-only: SELECT/WITH/PRAGMA only")
            else:
                st.dataframe(pd.DataFrame(q(sql)), use_container_width=True)

# ===================================================================================== Playground
elif page == "Playground":
    from compdoc.observability import start_run
    from compdoc.tools.registry import playground_tools
    st.title("Tool playground")
    caller = st.selectbox("Caller", ["system", "analyst_a", "analyst_b", "guest"])
    tools = playground_tools(caller)
    name = st.selectbox("Tool", sorted(tools))
    tool = tools[name]
    st.caption(tool.description)
    examples = {"get_trader_extract": {"trader_id": "T-1003"}, "get_term": {"trader_id": "T-1003", "path": "Compensation.Payout"},
                "list_trader_docs": {"trader_id": "T-1003"}, "search_sections": {"query": "clawback misconduct", "top_k": 5},
                "read_section": {"doc_id": "D-005", "section_id": "D-005:s2.2"}, "compare_versions": {"trader_id": "T-1003", "from_version": 1, "to_version": 2},
                "find_docs_with_term": {"term_path": "Clawback"}, "get_clause_variants": {"term_path": "Compensation.Payout"},
                "find_sections_by_term": {"trader_id": "T-1003", "path": "Deferral"}, "get_section_outline": {"doc_id": "D-005"},
                "verify_quote": {"doc_id": "D-005", "section_id": "D-005:s2.2", "quote": "fifteen percent (15%) of Net Trading P&L", "value": 15}}
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Input schema**")
        st.json(tool.schema(), expanded=False)
        args = st.text_area("Arguments (JSON)", json.dumps(examples.get(name, {}), indent=2), height=180)
        go = st.button("Call tool", type="primary")
    if go:
        with start_run("playground", caller=caller, input={"tool": name}):
            text, err = tool.run(json.loads(args))
        with c2:
            (st.error if err else st.success)("error" if err else "ok")
            st.json(json.loads(text), expanded=2)

# ===================================================================================== Prompts & evals
else:
    from compdoc.prompts import all_prompts
    st.title("Prompts & evals")
    used = {r["prompt_version"]: r["n"] for r in q("SELECT prompt_version, count(*) n FROM spans WHERE prompt_version IS NOT NULL GROUP BY prompt_version")}
    for name, (text, version) in all_prompts().items():
        with st.expander(f"{version} · used in {used.get(version, 0)} spans"):
            st.code(text, language="markdown")
    st.subheader("Eval results")
    res_dir = ROOT / "evals" / "results"
    files = sorted(res_dir.glob("*.json")) if res_dir.exists() else []
    rows = []
    for f in files:
        try:
            s = json.loads(f.read_text())["summary"]
            rows.append({"file": f.name, **s})
        except Exception:  # noqa: BLE001
            pass
    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, hide_index=True, use_container_width=True)
        f = st.selectbox("Details", [r["file"] for r in rows], index=len(rows) - 1)
        st.dataframe(pd.DataFrame(json.loads((res_dir / f).read_text())["traders"]), hide_index=True, use_container_width=True)
    else:
        st.info("Run `uv run python evals/run_evals.py` to produce results.")
