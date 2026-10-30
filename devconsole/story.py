"""Agent story: turns a run's spans into a per-agent narrative.

For each agent (or code step): what its job is, what it did (in order), every tool call with a readable
rendering of the response, and a "sections" table: which sections it found (and how), which it read,
which it used for findings / citations.
"""
from __future__ import annotations

import json
from collections import Counter

import pandas as pd
import streamlit as st

from compdoc.db import q, q1

ROLES = {
    "chat_router": ("🧭 Router", "LLM", "Reads the question and decides which skill should answer it."),
    "trader_qa": ("💬 Trader Q&A agent", "LLM agent", "Answers questions about one trader: published extract first, documents if needed."),
    "term_finder": ("🔎 Term Finder agent", "LLM agent", "Finds which documents/traders contain a term and its wording variants."),
    "drafter": ("📝 Drafting agent", "LLM agent", "Drafts a document from precedent clauses."),
    "build_citations": ("📎 Citation builder", "code", "Turns the sources the agent actually read into citations, keeping only those the answer refers to."),
    "orchestrator": ("🗂️ Orchestrator", "code", "Lists the trader's documents and decides which Term Researchers to start."),
    "reconciler": ("⚖️ Reconciler", "code + LLM", "Applies date rules to all findings; asks the LLM only about conflicts."),
    "summarize_publish": ("📰 Summarizer & publisher", "LLM + code", "Writes the summary and publishes a new extract version."),
    "human_review:requested": ("🙋 Human review requested", "pause", "Conflicts remain - the run pauses for a person."),
    "human_review:decided": ("✅ Human review decided", "person", "Reviewer decisions are applied and reconciliation re-run."),
}


def role(name: str) -> tuple[str, str, str]:
    if name.startswith("researcher:"):
        term = name.split(":", 1)[1]
        return (f"🔬 Term Researcher · {term}", "LLM agent",
                f"Searches ALL of the trader's documents for {term}, reads the relevant sections and records every value with a quote.")
    if name.startswith("digitizer:"):
        return (f"🏷️ Digitization agent · {name.split(':', 1)[1]}", "LLM", "Summarises and tags sections for navigation.")
    return ROLES.get(name, (name, "", ""))


def jl(s):
    try:
        return json.loads(s) if s else None
    except (json.JSONDecodeError, TypeError):
        return s


def _sec_info(section_ids: list[str]) -> dict[str, dict]:
    if not section_ids:
        return {}
    rows = q(f"SELECT s.section_id, s.number, s.heading, d.doc_id, d.title, d.doc_date FROM sections s JOIN documents d USING(doc_id)"
             f" WHERE s.section_id IN ({','.join('?' * len(section_ids))})", tuple(section_ids))
    return {r["section_id"]: r for r in rows}


# ------------------------------------------------------------------------------------------ grouping
def groups(run_id: str) -> list[dict]:
    spans = q("SELECT * FROM spans WHERE run_id=? ORDER BY started_at, seq", (run_id,))
    by_id = {s["span_id"]: s for s in spans}

    def owner(s: dict) -> str:
        cur = s
        while cur:
            if cur["kind"] == "agent":
                return cur["span_id"]
            parent = by_id.get(cur["parent_id"]) if cur["parent_id"] else None
            if parent is None:
                return cur["span_id"]
            # a "researcher" node only wraps its agent; let the agent own the work
            cur = parent
        return s["span_id"]

    out: dict[str, dict] = {}
    for s in spans:
        oid = owner(s)
        g = out.setdefault(oid, {"head": by_id[oid], "steps": []})
        if s["span_id"] != oid:
            g["steps"].append(s)
    # drop pure wrapper nodes whose only content is an agent (e.g. node "researcher")
    return sorted([g for g in out.values() if not (g["head"]["kind"] == "node" and g["head"]["name"] == "researcher")],
                  key=lambda g: g["head"]["started_at"])


# ------------------------------------------------------------------------------------------ tool renderers
def render_tool_response(name: str, args: dict, out, key: str) -> None:
    if out is None:
        st.caption("(no output)")
        return
    if isinstance(out, str):
        st.code(out[:3000])
        return
    if isinstance(out, dict) and out.get("error") and len(out) <= 3:
        st.error(out["error"])
        return
    try:
        if name in ("get_trader_extract",) and isinstance(out, dict):
            rows = [{"path": f"{t['term']}.{s['sub_term']}.{lf['key']}", "value": str(lf["value"]), "status": lf["status"],
                     "source": f"{lf['source']['doc_id']} §{lf['source']['section_id'].split(':s')[-1]}", "quote": lf["source"]["quote"]}
                    for t in out.get("terms", []) for s in t["sub_terms"] for lf in s["leaves"]]
            st.caption(f"Published extract v{out.get('version')} (built by run `{out.get('run_id')}`) - {len(rows)} values")
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        elif name == "get_term" and isinstance(out, dict):
            rows = [{"path": p, "value": str(lf["value"]), "status": lf["status"],
                     "source": f"{lf['source']['doc_id']} §{lf['source']['section_id'].split(':s')[-1]}", "quote": lf["source"]["quote"]}
                    for p, lf in (out.get("leaves") or {}).items()]
            st.caption(f"From published extract v{out.get('version')}")
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        elif name == "list_trader_docs" and isinstance(out, list):
            st.dataframe(pd.DataFrame([{k: d.get(k) for k in ("doc_id", "doc_date", "doc_type", "title", "terms_covered", "doc_summary")}
                                       for d in out]), hide_index=True, use_container_width=True)
        elif name == "get_section_outline" and isinstance(out, list):
            st.caption(f"Section outline of {args.get('doc_id')}")
            st.dataframe(pd.DataFrame([{"section": ("  " * max(0, o["level"] - 1)) + f"{o['number'] or ''} {o['heading']}",
                                        "pages": o["pages"], "tags": ", ".join(o["term_tags"]), "summary": o["summary"]} for o in out]),
                         hide_index=True, use_container_width=True)
        elif name == "get_section_outlines" and isinstance(out, list):
            for o in out:
                st.markdown(f"**{o.get('doc_id')} · {o.get('title')} · {o.get('doc_date')}** - {len(o.get('outline', []))} sections")
                st.dataframe(pd.DataFrame([{"section": ("    " * max(0, s['level'] - 1)) + f"{s['number'] or ''} {s['heading']}",
                                            "tags": ", ".join(s["term_tags"]), "summary": s["summary"]} for s in o.get("outline", [])]),
                             hide_index=True, use_container_width=True)
        elif name == "read_chosen_sections" and isinstance(out, list):
            st.caption(f"{len(out)} section(s) chosen from the outlines")
            for sec in out:
                if sec.get("error"):
                    st.error(f"{sec.get('section_id')}: {sec['error']}")
                    continue
                doc = sec.get("doc") or {}
                st.markdown(f"**{doc.get('doc_id')} §{sec.get('number') or ''} {sec.get('heading')}** ({doc.get('doc_date')}) - "
                            f"*chosen because:* {sec.get('reason')}")
                st.code((sec.get("text") or "")[:1200], language=None)
        elif name == "read_section" and isinstance(out, dict):
            doc = out.get("doc") or {}
            st.markdown(f"**{doc.get('doc_id')} · {doc.get('title')} · {doc.get('doc_date')} · §{out.get('number') or ''} "
                        f"{out.get('heading')}** · pages {out.get('pages')}")
            if out.get("text"):
                st.code(out["text"][:2500], language=None)
            for ch in out.get("children", [])[:12]:
                st.markdown(f"↳ §{ch['number']} {ch['heading']}")
                st.code(ch["text"][:800], language=None)
            for t in out.get("tables", []):
                st.markdown(t)
            if out.get("definitions"):
                st.caption("Definitions attached: " + " | ".join(out["definitions"]))
        elif name in ("search_sections", "find_sections_by_term") and isinstance(out, list):
            st.dataframe(pd.DataFrame([{k: h.get(k) for k in ("doc_id", "doc_date", "section_id", "number", "heading", "score", "summary", "snippet")
                                        if k in h} for h in out]), hide_index=True, use_container_width=True)
        elif name == "find_docs_with_term" and isinstance(out, dict):
            st.caption(f"{out.get('count')} sections in {out.get('documents')} documents")
            st.dataframe(pd.DataFrame([{k: h.get(k) for k in ("doc_id", "trader_id", "doc_date", "number", "heading", "summary")}
                                       for h in out.get("results", [])]), hide_index=True, use_container_width=True)
        elif name == "get_clause_variants" and isinstance(out, dict):
            for v in out.get("variants", []):
                st.markdown(f"`{v['variant_id']}` · {v['doc_count']} docs · latest {v['latest_doc_date']} ({v['latest_doc_id']})")
                st.caption(v.get("canonical_text") or "[withheld]")
        elif name == "record_finding" and isinstance(out, dict):
            ok = "✅ recorded" if out.get("recorded") else "❌ rejected"
            st.markdown(f"{ok} `{args.get('path')}` = **{out.get('value', args.get('value'))}** · quote verified: {out.get('quote_verified')}"
                        + (f" · {out.get('error')}" if out.get("error") else ""))
            st.caption(f"“{args.get('quote')}” — {args.get('doc_id')} {args.get('section_id')}")
            for w in out.get("warnings", []):
                st.warning(w)
        elif name == "build_citations" and isinstance(out, list):
            st.dataframe(pd.DataFrame([{"#": c["n"], "doc": c["doc_id"], "section": c["section"], "page": c.get("page"),
                                        "docling item": c.get("docling_ref"), "quote": c.get("quote")} for c in out]),
                         hide_index=True, use_container_width=True)
        elif name == "reconcile" and isinstance(out, dict):
            st.markdown(f"{out.get('leaves')} values reconciled · conflicts: {out.get('conflicts') or 'none'}")
        else:
            st.json(out, expanded=1)
    except Exception:  # noqa: BLE001 - never let a renderer break the page
        st.json(out, expanded=1)


# ------------------------------------------------------------------------------------------ sections table
def sections_table(steps: list[dict], cited: set[str]) -> pd.DataFrame | None:
    found: dict[str, set] = {}
    read: set[str] = set()
    reasons: dict[str, str] = {}
    used: dict[str, list] = {}
    for s in steps:
        if s["kind"] != "tool":
            continue
        args, out = jl(s["input_json"]) or {}, jl(s["output_json"])
        n = s["name"]
        if n in ("search_sections", "find_sections_by_term") and isinstance(out, list):
            for h in out:
                found.setdefault(h["section_id"], set()).add(n)
        elif n == "find_docs_with_term" and isinstance(out, dict):
            for h in out.get("results", []):
                found.setdefault(h["section_id"], set()).add(n)
        elif n == "get_section_outline" and isinstance(out, list):
            for o in out:
                found.setdefault(o["section_id"], set()).add("outline")
        elif n == "get_section_outlines" and isinstance(out, list):
            for o in out:
                for s2 in o.get("outline", []):
                    found.setdefault(s2["section_id"], set()).add("outline")
        elif n == "read_chosen_sections":
            for c in args.get("choices", []):
                read.add(c["section_id"])
                reasons[c["section_id"]] = c.get("reason") or ""
                found.setdefault(c["section_id"], set()).add("chosen from outline")
        elif n == "read_section":
            sid = args.get("section_id")
            if sid:
                read.add(sid)
                found.setdefault(sid, set())
        elif n == "record_finding" and isinstance(out, dict):
            sid = args.get("section_id")
            if sid:
                used.setdefault(sid, []).append(f"{args.get('path', '').split('.')[-1]}={out.get('value', args.get('value'))}"
                                                + ("" if out.get("recorded") else " (rejected)"))
        elif n in ("get_trader_extract", "get_term") and isinstance(out, dict):
            leaves = ([lf for t in out.get("terms", []) for s2 in t["sub_terms"] for lf in s2["leaves"]] if n == "get_trader_extract"
                      else list((out.get("leaves") or {}).values()))
            for lf in leaves:
                found.setdefault(lf["source"]["section_id"], set()).add("published extract")
    if not found:
        return None
    info = _sec_info(list(found))
    rows = []
    for sid, via in found.items():
        i = info.get(sid, {})
        rows.append({"section": sid, "doc date": i.get("doc_date"), "heading": f"{i.get('number') or ''} {i.get('heading') or ''}".strip(),
                     "found via": ", ".join(sorted(via)) or "-", "read": "✓" if sid in read else "",
                     "why chosen": reasons.get(sid, ""),
                     "used for": "; ".join(used.get(sid, [])), "cited in answer": "✓" if sid in cited else ""})
    df = pd.DataFrame(rows)
    return df.sort_values(["read", "used for", "cited in answer"], ascending=False).reset_index(drop=True)


# ------------------------------------------------------------------------------------------ render
def what_it_did(head: dict, steps: list[dict]) -> str:
    tools = Counter(s["name"] for s in steps if s["kind"] == "tool")
    turns = sum(1 for s in steps if s["kind"] == "llm")
    out = jl(head["output_json"])
    parts = []
    if head["kind"] == "llm" and head["name"] == "chat_router" and isinstance(out, dict):
        parts.append(f"Decided this is a **{out.get('route')}** question (trader `{out.get('trader_id')}`, term `{out.get('term_path')}`).")
    if turns:
        parts.append(f"{turns} model turn(s).")
    if tools:
        parts.append("Tools: " + ", ".join(f"`{k}` ×{v}" for k, v in tools.items()) + ".")
    outl = [s for s in steps if s["kind"] == "tool" and s["name"] == "get_section_outlines"]
    if outl:
        docs_n = sum(len(jl(s["input_json"]).get("doc_ids", [])) for s in outl if jl(s["input_json"]))
        parts.append(f"Scanned the outlines of {docs_n} document(s).")
    chosen = [c for s in steps if s["kind"] == "tool" and s["name"] == "read_chosen_sections" and jl(s["input_json"])
              for c in jl(s["input_json"]).get("choices", [])]
    if chosen:
        parts.append(f"Chose {len(chosen)} section(s) from them.")
    reads = [jl(s["input_json"]).get("section_id") for s in steps if s["kind"] == "tool" and s["name"] == "read_section" and jl(s["input_json"])]
    reads += [c["section_id"] for c in chosen]
    if reads:
        parts.append(f"Read {len(reads)} section(s).")
    rec = [s for s in steps if s["kind"] == "tool" and s["name"] == "record_finding"]
    if rec:
        parts.append(f"Recorded {sum(1 for s in rec if (jl(s['output_json']) or {}).get('recorded'))} finding(s).")
    if head["name"] in ("get_trader_extract", "get_term") or tools.get("get_trader_extract") or tools.get("get_term"):
        if not reads:
            parts.append("Answered from the **published extract** - no document sections were opened in this run "
                         "(see *How these values were extracted* below).")
    if isinstance(out, dict) and out.get("final"):
        parts.append(f"Final message: “{str(out['final'])[:300]}”")
    if head["kind"] == "tool" and head["name"] == "build_citations" and isinstance(out, list):
        parts.append(f"Kept {len(out)} citation(s).")
    return " ".join(parts) or "—"


def render_story(run_id: str, key: str = "s") -> None:
    run = q1("SELECT * FROM runs WHERE run_id=?", (run_id,))
    cited = {c["section_id"] for c in ((jl(run["output_json"]) or {}).get("citations") or [])} if run else set()
    step_no = 0
    for gi, g in enumerate(groups(run_id), 1):
        head, steps = g["head"], g["steps"]
        title, kind, job = role(head["name"])
        tok = sum((s["tokens_in"] or 0) + (s["tokens_out"] or 0) for s in [head] + steps)
        secs = round((head["latency_ms"] or 0) / 1000, 1)
        st.markdown(f"### {gi}. {title}  \n<span style='color:gray'>{kind} · {secs}s{f' · {tok:,} tokens' if tok else ''}"
                    f"{' · ' + head['model'] if head['model'] else ''}</span>", unsafe_allow_html=True)
        if job:
            st.markdown(f"*Job:* {job}")
        st.markdown(f"*What it did:* {what_it_did(head, steps)}")
        if head["error"]:
            st.error(head["error"])
        if head["kind"] == "tool":  # code step with no children (e.g. citation builder)
            step_no += 1
            with st.expander(f"Step {step_no} · 🔧 {head['name']} - response", expanded=True):
                render_tool_response(head["name"], jl(head["input_json"]) or {}, jl(head["output_json"]), f"{key}-{gi}-h")
        for s in steps:
            step_no += 1
            args, out = jl(s["input_json"]) or {}, jl(s["output_json"])
            if s["kind"] == "llm":
                calls = (out or {}).get("function_calls") if isinstance(out, dict) else None
                if calls:
                    label = "decided to call " + ", ".join(f"{c['name']}" for c in calls)
                elif isinstance(out, dict) and (out.get("text") or out.get("content")):
                    label = "decided it had enough → wrote the answer"
                else:
                    label = "LLM output"
                with st.expander(f"Step {step_no} · 💬 model {label}", expanded=False):
                    if calls:
                        for c in calls:
                            st.code(f"{c['name']}({c.get('arguments')})", language="json")
                    elif isinstance(out, dict) and (out.get("text") or out.get("content")):
                        st.markdown(out.get("text") or out.get("content"))
                    else:
                        st.json(out, expanded=1)
                    st.caption("Full prompt/messages sent are in the Run detail page (input of this span).")
            elif s["kind"] == "tool":
                a = {k: v for k, v in args.items() if k != "args"} if isinstance(args, dict) else args
                short = json.dumps(a, default=str)
                with st.expander(f"Step {step_no} · 🔧 {s['name']}({short[:110]}{'…' if len(short) > 110 else ''})", expanded=False):
                    render_tool_response(s["name"], a if isinstance(a, dict) else {}, out, f"{key}-{s['span_id']}")
            elif s["kind"] in ("node", "agent"):
                with st.expander(f"Step {step_no} · {s['kind']} {s['name']}", expanded=False):
                    st.json(out, expanded=1)
        df = sections_table(steps + ([head] if head["kind"] == "tool" else []), cited)
        if df is not None:
            st.markdown("**Sections this agent looked at**")
            st.dataframe(df, hide_index=True, use_container_width=True)
        st.divider()


def extract_run_behind(run_id: str) -> list[tuple[str, str, int]]:
    """Extraction runs that produced the published extracts this run read: [(trader_id, extract_run_id, version)]."""
    out = []
    for s in q("SELECT name, input_json, output_json FROM spans WHERE run_id=? AND kind='tool' AND name IN ('get_trader_extract','get_term')", (run_id,)):
        o, a = jl(s["output_json"]) or {}, jl(s["input_json"]) or {}
        tid = (o.get("trader_id") if isinstance(o, dict) else None) or a.get("trader_id")
        ver = o.get("version") if isinstance(o, dict) else None
        if tid and ver:
            r = q1("SELECT run_id FROM extracts WHERE trader_id=? AND version=?", (tid, ver))
            if r and r["run_id"] and (tid, r["run_id"], ver) not in out:
                out.append((tid, r["run_id"], ver))
    return out
