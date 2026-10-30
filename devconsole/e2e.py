"""End-to-end view: for one trader, ingestion of every document → the extraction run behind the current term tree →
questions asked against it, with each citation traced back through every stage to the source file."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from compdoc.db import q, q1


def jl(s):
    try:
        return json.loads(s) if s else None
    except (json.JSONDecodeError, TypeError):
        return s


def _cost(rows) -> tuple[int, float]:
    return sum((r["tokens_in"] or 0) + (r["tokens_out"] or 0) for r in rows), sum(r["cost"] or 0 for r in rows)


# ------------------------------------------------------------------------------------------ data
def ingestion_rows(tid: str) -> list[dict]:
    out = []
    for d in q("SELECT d.*, c.status conv_status, c.seconds conv_s, c.qc_json, e.model enr_model, e.enriched_at, e.doc_summary "
               "FROM documents d LEFT JOIN conversions c ON c.file_hash=d.file_hash LEFT JOIN doc_enrichment e USING(doc_id) "
               "WHERE d.trader_id=? ORDER BY d.doc_date, d.doc_id", (tid,)):
        qc = jl(d["qc_json"]) or {}
        did = d["doc_id"]
        n_sec = q1("SELECT count(*) n FROM sections WHERE doc_id=? AND level > 0", (did,))["n"]
        try:
            n_vec = q1("SELECT count(*) n FROM section_vectors v JOIN section_text t ON t.section_hash=v.section_hash WHERE t.doc_id=?",
                       (did,))["n"]
        except Exception:  # noqa: BLE001
            n_vec = 0
        llm = q("SELECT tokens_in, tokens_out, cost FROM spans WHERE kind='llm' AND (name=? OR name LIKE ? OR name LIKE ? OR name=?)",
                (f"digitizer:{did}", f"digitizer:{did}:%", f"digitizer-article:{did}:%", f"digitizer-doc:{did}"))
        tok, cost = _cost(llm)
        out.append({"doc_id": did, "type": d["doc_type"], "date": d["doc_date"], "format": d["format"],
                    "pages": qc.get("pages") or None, "docling": d["conv_status"], "convert s": d["conv_s"],
                    "sharded": bool(qc.get("sharding")), "sections": n_sec, "QC flags": ", ".join(qc.get("flags") or []),
                    "enriched by": d["enr_model"], "enrich tokens": tok, "enrich $": round(cost, 4), "embedding chunks": n_vec,
                    "ingested": (d["ingested_at"] or "")[:16], "summary": (d["doc_summary"] or "")[:140]})
    return out


def extraction_info(tid: str) -> dict | None:
    ext = q1("SELECT * FROM extracts WHERE trader_id=? ORDER BY version DESC LIMIT 1", (tid,))
    if not ext:
        return None
    rid = ext["run_id"]
    run = q1("SELECT * FROM runs WHERE run_id=?", (rid,))
    items = jl((q1("SELECT output_json FROM spans WHERE run_id=? AND name='route_work_items' ORDER BY started_at LIMIT 1", (rid,))
                or {}).get("output_json")) or []
    readers = []
    for n in q("SELECT output_json FROM spans WHERE run_id=? AND kind='node' AND name='doc_reader' ORDER BY started_at", (rid,)):
        for rep in (jl(n["output_json"]) or {}).get("reports", []):
            readers.append(rep)
    agent_spans = {s["name"]: s for s in q("SELECT name, tokens_in, tokens_out, cost, latency_ms FROM spans WHERE run_id=? AND kind='agent'",
                                          (rid,))}
    llm = q("SELECT tokens_in, tokens_out, cost FROM spans WHERE run_id=? AND kind='llm'", (rid,))
    gaps = jl((q1("SELECT output_json FROM spans WHERE run_id=? AND name='coverage_check' ORDER BY started_at DESC LIMIT 1", (rid,))
               or {}).get("output_json")) or []
    extract = jl(ext["extract_json"]) or {}
    n_find = q1("SELECT count(*) n FROM findings WHERE run_id=?", (rid,))["n"]
    reviews = q("SELECT status, reviewer, decided_at FROM reviews WHERE run_id=?", (rid,))
    tok, cost = _cost(llm)
    leaves = sum(len(s["leaves"]) for t in extract.get("terms", []) for s in t["sub_terms"])
    return {"run": run, "run_id": rid, "version": ext["version"], "items": items, "readers": readers, "agent_spans": agent_spans,
            "gaps": gaps, "extract": extract, "findings": n_find, "reviews": reviews, "tokens": tok, "cost": cost, "leaves": leaves,
            "legacy": not items}


def chat_runs(tid: str) -> list[dict]:
    return q("SELECT run_id, started_at, input_json, output_json, latency_ms, tokens_in, tokens_out, cost FROM runs WHERE type='chat' "
             "AND (trader_id=? OR input_json LIKE ?) ORDER BY started_at DESC LIMIT 50", (tid, f"%{tid}%"))


# ------------------------------------------------------------------------------------------ flow diagram
def flow_dot(tid: str, ing: list[dict], ex: dict | None, chat: dict | None) -> str:
    docs = len(ing)
    pages = sum(r["pages"] or 0 for r in ing)
    secs = sum(r["sections"] for r in ing)
    etok = sum(r["enrich tokens"] for r in ing)
    ecost = sum(r["enrich $"] for r in ing)
    ok = "#c8e6c9"
    lines = ["digraph G { rankdir=LR; compound=true; node [shape=box, style=\"rounded,filled\", fontname=Helvetica, fontsize=10, fillcolor=\"#f5f5f5\"];",
             "subgraph cluster_i { label=\"1 · INGESTION (per document, cached)\"; fontname=Helvetica; fontsize=11; color=\"#9e9e9e\";",
             f'i1 [label="Source DB\\n{docs} documents", fillcolor="{ok}"];',
             f'i2 [label="Docling\\n{pages or "?"} pages", fillcolor="{ok}"];',
             f'i3 [label="Section builder\\n{secs} sections", fillcolor="{ok}"];',
             f'i4 [label="Enrichment (LLM)\\n{etok:,} tokens · ${ecost:.3f}", fillcolor="{ok if etok or ing else "#eeeeee"}"];',
             f'i5 [label="Text store +\\nembeddings", fillcolor="{ok}"];',
             "i1 -> i2 -> i3 -> i4 -> i5; }"]
    if ex:
        n_items = len(ex["items"]) or len(ex["readers"])
        lines += ["subgraph cluster_x { label=\"2 · EXTRACTION (one run, current term tree)\"; fontname=Helvetica; fontsize=11; color=\"#9e9e9e\";",
                  f'x1 [label="Coverage router\\n{n_items} work items", fillcolor="{ok}"];',
                  f'x2 [label="Doc Readers\\n{len(ex["readers"]) or "-"} agents\\n{ex["findings"]} findings", fillcolor="{ok}"];',
                  f'x3 [label="Coverage check\\n{len(ex["gaps"])} gaps", fillcolor="{ok}"];',
                  f'x4 [label="Reconcile\\n{len(ex["extract"].get("conflicts", []))} conflicts", fillcolor="{ok}"];',
                  f'x5 [label="Published v{ex["version"]}\\n{ex["leaves"]} values\\n{ex["tokens"]:,} tok · ${ex["cost"]:.3f}", fillcolor="{ok}"];',
                  "x1 -> x2 -> x3 -> x4 -> x5; }", "i5 -> x1 [lhead=cluster_x, ltail=cluster_i];"]
    else:
        lines.append('x5 [label="2 · EXTRACTION\\nnot run yet", fillcolor="#eeeeee"]; i5 -> x5;')
    if chat:
        out = jl(chat["output_json"]) or {}
        lines += ["subgraph cluster_q { label=\"3 · QUERY (chat)\"; fontname=Helvetica; fontsize=11; color=\"#9e9e9e\";",
                  f'q1 [label="Question\\nroute: {out.get("route", "?")}", fillcolor="{ok}"];',
                  f'q2 [label="Answer\\n{len(out.get("citations") or [])} citations\\n${chat["cost"] or 0:.4f}", fillcolor="{ok}"];',
                  "q1 -> q2; }", ("x5" if ex else "x5") + " -> q1 [lhead=cluster_q];"]
    else:
        lines.append('q1 [label="3 · QUERY\\nask a question below", fillcolor="#eeeeee"]; x5 -> q1;')
    lines.append("}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------ provenance
def provenance(citations: list[dict], ex: dict | None) -> pd.DataFrame:
    rows = []
    for c in citations:
        f = q("SELECT finding_id, path, value_json, agent, quote_verified FROM findings WHERE run_id=? AND json_extract(source_json,'$.section_id')=?",
              (ex["run_id"], c["section_id"])) if ex else []
        span = q1("SELECT item_ref, page FROM section_spans WHERE section_id=? ORDER BY start LIMIT 1", (c["section_id"],))
        d = q1("SELECT d.path, d.file_hash, c.status, c.seconds FROM documents d LEFT JOIN conversions c ON c.file_hash=d.file_hash "
               "WHERE d.doc_id=?", (c["doc_id"],))
        rows.append({
            "#": c["n"],
            "③ cited in answer": f"{c['doc_id']} §{c['section']}",
            "② recorded by extraction": "; ".join(f"{r['path'].split('.')[-1]}={jl(r['value_json'])} ({r['agent'].split(':')[0] if r['agent'] else '-'}, "
                                                   f"{'verified' if r['quote_verified'] else 'UNVERIFIED'})" for r in f) or "(not a recorded value)",
            "① section → Docling item": f"{c['section_id']} → {c.get('docling_ref') or (span or {}).get('item_ref') or '-'}",
            "page": c.get("page") or (span or {}).get("page"),
            "source file": Path(d["path"]).name if d else "-",
            "file hash": (d["file_hash"] or "")[:12] if d else "-",
            "Docling": f"{d['status']} in {d['seconds']}s" if d and d["status"] else "-",
            "quote": (c.get("quote") or "")[:160],
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------ page
def render_e2e() -> None:
    st.title("End-to-end")
    st.caption("One trader, all phases: **1 · ingestion** of every document → **2 · extraction** run that built the current term "
               "tree → **3 · query** against it. Each answer's citations are traced back through every stage to the source file.")
    traders = q("SELECT trader_id, max(trader_name) name, count(*) docs FROM documents GROUP BY trader_id ORDER BY trader_id")
    if not traders:
        st.info("No documents yet. Run `compdoc ingest`.")
        return
    ids = [t["trader_id"] for t in traders]
    tid = st.selectbox("Trader", ids, format_func=lambda x: next(f"{t['trader_id']} · {t['name']} · {t['docs']} documents"
                                                                 for t in traders if t["trader_id"] == x))
    ing = ingestion_rows(tid)
    ex = extraction_info(tid)
    runs = chat_runs(tid)

    # question: pick a previous one or ask a new one
    c1, c2 = st.columns([3, 1])
    question = c1.text_input("Ask a question about this trader (runs the chat live)", placeholder=f"e.g. What is {tid}'s payout and when did it change?")
    if c2.button("Ask", type="primary", disabled=not question):
        from compdoc.agents.chat import chat_turn
        with st.spinner("Answering…"):
            try:
                out = chat_turn(question if tid in question else f"{question} ({tid})", "system")
                st.session_state["e2e_chat"] = out["run_id"]
            except Exception as e:  # noqa: BLE001
                st.error(f"{type(e).__name__}: {e}")
        runs = chat_runs(tid)
    chat = None
    if runs:
        rids = [r["run_id"] for r in runs]
        cur = st.session_state.get("e2e_chat")
        sel = st.selectbox("Question shown", rids, index=rids.index(cur) if cur in rids else 0,
                           format_func=lambda r: next(f"{x['started_at'][:16]} · {(jl(x['input_json']) or {}).get('message', '')[:90]}"
                                                      for x in runs if x["run_id"] == r))
        chat = next(r for r in runs if r["run_id"] == sel)

    st.graphviz_chart(flow_dot(tid, ing, ex, chat), use_container_width=True)

    # ---- 1 ingestion
    st.header("1 · Ingestion")
    tot_pages = sum(r["pages"] or 0 for r in ing)
    m = st.columns(5)
    m[0].metric("Documents", len(ing))
    m[1].metric("Pages (PDF)", tot_pages)
    m[2].metric("Sections", sum(r["sections"] for r in ing))
    m[3].metric("Enrichment tokens", f"{sum(r['enrich tokens'] for r in ing):,}")
    m[4].metric("Enrichment cost", f"${sum(r['enrich $'] for r in ing):.3f}")
    st.caption("Database row → Docling → section builder → quality checks → text store → LLM enrichment → embeddings. "
               "Pick a document to open its full 10-step lifecycle.")
    st.dataframe(pd.DataFrame(ing), hide_index=True, use_container_width=True)
    d1, d2 = st.columns([3, 1])
    doc_pick = d1.selectbox("Document", [r["doc_id"] for r in ing], key="e2e_doc")
    if d2.button("Open its lifecycle →"):
        st.session_state.update(lc_doc=doc_pick, page="Document lifecycle")
        st.rerun()

    # ---- 2 extraction
    st.header("2 · Extraction")
    if not ex:
        last = q1("SELECT run_id, status, started_at FROM runs WHERE type='extract' AND trader_id=? ORDER BY started_at DESC LIMIT 1", (tid,))
        if last and last["status"] == "interrupted":
            pend = q1("SELECT payload_json FROM reviews WHERE run_id=? AND status='pending'", (last["run_id"],))
            n = len(jl(pend["payload_json"]) or []) if pend else 0
            st.warning(f"Extraction run `{last['run_id']}` is **paused for human review** ({n} conflict(s)); nothing is published yet. "
                       "Resolve it on the business UI's Review tab, then this section shows the published term tree.")
            with st.expander("Every agent and tool step of the paused run"):
                from story import render_story
                render_story(last["run_id"], key="e2e-p")
        elif last:
            st.info(f"Latest extraction run `{last['run_id']}` ended with status {last['status']}; nothing published.")
        else:
            st.info(f"No extraction yet for {tid}. Run it from the business UI or `compdoc extract {tid}`.")
    else:
        run = ex["run"] or {}
        m = st.columns(6)
        m[0].metric("Published version", f"v{ex['version']}")
        m[1].metric("Values", ex["leaves"])
        m[2].metric("Findings recorded", ex["findings"])
        m[3].metric("Conflicts", len(ex["extract"].get("conflicts", [])))
        m[4].metric("Seconds", round((run.get("latency_ms") or 0) / 1000, 1))
        m[5].metric("LLM cost", f"${ex['cost']:.4f}")
        st.caption(f"Run `{ex['run_id']}` · status {run.get('status')} · provider of that run: "
                   f"{'offline rules' if not ex['tokens'] else 'LLM'}"
                   + (" · built before the Doc Reader design (term researchers)" if ex["legacy"] else ""))
        if ex["items"]:
            st.markdown("**Coverage router → work items** (one Doc Reader each)")
            st.dataframe(pd.DataFrame([{"document": w["doc_id"], "terms": ", ".join(w["terms"]), "shortlist": w["shortlist"],
                                        "evidence": ", ".join(f"{k}: {v}" for k, v in w["evidence"].items())} for w in ex["items"]]),
                         hide_index=True, use_container_width=True)
        if ex["readers"]:
            st.markdown("**Doc Readers**")
            rows = []
            for rep in ex["readers"]:
                name = f"doc_reader:{rep['doc_id']}:{'+'.join(rep['terms'])}" + (f":r{rep['round']}" if rep.get("round", 1) > 1 else "")
                sp = ex["agent_spans"].get(name) or {}
                rows.append({"document": rep["doc_id"], "terms": ", ".join(rep["terms"]), "round": rep.get("round", 1),
                             "sections read": len(rep["read"]), "values recorded": rep["recorded"], "tool calls": rep["tool_calls"],
                             "tokens": (sp.get("tokens_in") or 0) + (sp.get("tokens_out") or 0), "summary": rep["summary"][:160]})
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        if ex["gaps"]:
            st.warning("Coverage gaps re-queued: " + "; ".join(f"{g['doc_id']} {', '.join(g['terms'])}" for g in ex["gaps"]))
        conf = ex["extract"].get("conflicts", [])
        if conf:
            st.markdown("**Conflicts**")
            st.dataframe(pd.DataFrame([{"path": c["path"], "kind": c["kind"], "resolution": (c.get("resolution") or {}).get("action", "open"),
                                        "by": (c.get("resolution") or {}).get("by")} for c in conf]), hide_index=True, use_container_width=True)
        with st.expander("Current term tree (published)"):
            st.dataframe(pd.DataFrame([{"path": f"{t['term']}.{s['sub_term']}.{lf['key']}", "value": str(lf["value"]), "status": lf["status"],
                                        "source": f"{lf['source']['doc_id']} {lf['source']['effective_date']} §{lf['source']['section_id'].split(':s')[-1]}"}
                                       for t in ex["extract"].get("terms", []) for s in t["sub_terms"] for lf in s["leaves"]]),
                         hide_index=True, use_container_width=True)
        with st.expander("Every agent and tool step of this extraction"):
            from story import render_story
            render_story(ex["run_id"], key="e2e-x")

    # ---- 3 query
    st.header("3 · Query")
    if not chat:
        st.info("No questions asked about this trader yet. Ask one above.")
        return
    out = jl(chat["output_json"]) or {}
    m = st.columns(4)
    m[0].metric("Route", out.get("route", "-"))
    m[1].metric("Citations", len(out.get("citations") or []))
    m[2].metric("Seconds", round((chat["latency_ms"] or 0) / 1000, 1))
    m[3].metric("Cost", f"${chat['cost'] or 0:.4f}")
    st.markdown(f"**Question:** {(jl(chat['input_json']) or {}).get('message', '')}")
    st.markdown(out.get("answer", ""))
    cites = out.get("citations") or []
    if cites:
        st.markdown("**Provenance: each citation traced back through the three phases**")
        st.caption("③ where the answer cites it · ② the value the extraction run recorded from that section (agent, quote check) · "
                   "① the section, the Docling item it was built from, the page, and the source file and conversion.")
        st.dataframe(provenance(cites, ex), hide_index=True, use_container_width=True)
    with st.expander("Every agent and tool step of this answer"):
        from story import render_story
        render_story(chat["run_id"], key="e2e-q")
