"""CLI: compdoc ingest | extract | review | show | chat | find | draft | traders"""
from __future__ import annotations

import json
import logging

import structlog
import typer
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
app = typer.Typer(add_completion=False, help="Agentic trader comp contract extractor")
con = Console()


@app.command()
def ingest(force: bool = False, no_enrich: bool = False, doc: list[str] = typer.Option(None, help="Only these doc ids")):
    """Register docs from the source DB, digitize with Docling, build sections, enrich."""
    from .service import ingest_all
    res = ingest_all(enrich=not no_enrich, force=force, doc_ids=doc or None)
    t = Table("doc", "sections", "qc flags", "enrichment")
    for r in res:
        t.add_row(r["doc_id"], str(r.get("sections", r.get("skipped", ""))), ",".join(r.get("qc_flags", []) or []),
                  json.dumps(r.get("enrichment", {}))[:70])
    con.print(t)


def print_extract(ex: dict) -> None:
    tree = Tree(f"[bold]{ex['trader_id']}[/] v{ex.get('version')} as of {ex['as_of']}")
    for t in ex["terms"]:
        tn = tree.add(f"[cyan]{t['term']}")
        for s in t["sub_terms"]:
            sn = tn.add(s["sub_term"])
            for lf in s["leaves"]:
                src = lf["source"]
                hist = " ← " + ", ".join(f"{h['value']} ({h['doc_id']} {h['effective_date']})" for h in lf["history"]) if lf["history"] else ""
                color = {"current": "green", "removed": "yellow"}.get(lf["status"], "red")
                sn.add(f"{lf['key']} = [{color}]{lf['value']}[/] {lf.get('unit') or ''} [{lf['status']}] "
                       f"[dim]{src['doc_id']} {src.get('effective_date')} {src['section_id'].split(':')[-1]} p{src.get('page')}{hist}[/]")
    con.print(tree)
    if ex.get("summary"):
        con.print(f"\n[bold]Summary[/]: {ex['summary']}")


@app.command()
def extract(trader_id: str, caller: str = "system"):
    """Run the multi-agent extraction for a trader."""
    from .service import extract_trader
    res = extract_trader(trader_id, caller)
    con.print(f"run [bold]{res['run_id']}[/] → {res['status']}")
    if res["status"] == "pending_review":
        for it in res["review"]["items"]:
            con.print(f"[red]REVIEW[/] {it['kind']} {it['path']}: {it['note']}")
            for c in it["candidates"]:
                con.print(f"   finding {c['finding_id']}: {c['value']} ({c['doc_id']} {c['effective_date']}) scope={c['scope_note']}")
        con.print(f"Resolve with: compdoc review {res['run_id']} --pick PATH=FINDING_ID ... --reviewer NAME")
    else:
        print_extract(res["extract"])


@app.command()
def review(run_id: str, pick: list[str] = typer.Option(None, help="PATH=FINDING_ID"),
           value: list[str] = typer.Option(None, help="PATH=VALUE (manual)"), reviewer: str = "reviewer", reason: str = "CLI review"):
    """Resume a run paused for human review."""
    from .service import resume_review
    res: dict = {}
    for p in pick or []:
        k, v = p.split("=", 1)
        res[k] = {"action": "pick", "finding_id": int(v), "reason": reason}
    for p in value or []:
        k, v = p.split("=", 1)
        res[k] = {"action": "manual", "value": json.loads(v) if v[:1] in "0123456789[{tf\"n" else v, "reason": reason}
    out = resume_review(run_id, res, reviewer)
    con.print(f"run {out['run_id']} → {out['status']}")
    if out["status"] == "published":
        print_extract(out["extract"])


@app.command()
def show(trader_id: str, version: int = None):
    """Print the latest published extract."""
    from .service import get_extract
    ex = get_extract(trader_id, version=version)
    print_extract(ex.model_dump()) if ex else con.print("no extract yet")


@app.command()
def traders():
    from .service import traders as t
    tb = Table("trader", "name", "docs", "first", "last")
    for r in t():
        tb.add_row(r["trader_id"], r["trader_name"], str(r["docs"]), r["first_doc"], r["last_doc"])
    con.print(tb)


@app.command()
def chat(message: str, caller: str = "system"):
    """One chat turn (router → trader Q&A / term finder / drafting)."""
    from .agents.chat import chat_turn
    from .tools.citations import format_citations
    out = chat_turn(message, caller=caller)
    con.print(f"[dim]route: {out['route']} · run {out['run_id']}[/]\n{out['answer']}")
    if out["citations"]:
        con.print("\n[bold]Citations[/]")
        con.print(format_citations(out["citations"]), markup=False)


@app.command()
def find(term_path: str, caller: str = "system"):
    """Documents containing a term, newest first (+ wording variants)."""
    from .tools.clauses import find_docs_with_term, get_clause_variants
    res = find_docs_with_term(term_path, caller=caller)
    t = Table("doc", "trader", "date", "type", "section", "summary")
    for r in res["results"]:
        t.add_row(r["doc_id"], r["trader_id"], r["doc_date"], r["doc_type"], f"{r['number']} {r['heading']}", (r["summary"] or "[withheld]")[:70])
    con.print(t)
    for v in get_clause_variants(term_path, caller=caller)["variants"]:
        con.print(f"[bold]{v['variant_id']}[/] docs={v['doc_count']} latest={v['latest_doc_date']}: {(v['canonical_text'] or '[withheld]')[:150]}")


@app.command()
def draft(trader_id: str, request: str, caller: str = "system"):
    """Draft an amendment/side letter from precedent language."""
    from .agents.drafter import run_drafter
    out = run_drafter(trader_id, request, caller)
    con.print(out)


if __name__ == "__main__":
    app()
