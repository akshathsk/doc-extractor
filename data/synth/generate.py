"""Generate synthetic trader compensation contracts + gold labels.

Output (data/synth/out/):
  docs/<doc_id>.pdf|.docx      contract files
  manifest.json                the "source DB": one row per document (doc_id, trader_id, title, doc_type, doc_date, path)
  gold/<trader_id>.json        expected current leaf values + expected conflicts

Nothing here is real. Names, firms and numbers are invented.
"""
from __future__ import annotations

import json
import random
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document as DocxDocument
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path(__file__).parent / "out"
FIRM = "Meridian Ridge Capital LP"

NUM_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
    10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen", 16: "sixteen",
    17: "seventeen", 18: "eighteen", 19: "nineteen", 20: "twenty", 22: "twenty-two", 24: "twenty-four",
    25: "twenty-five", 30: "thirty", 35: "thirty-five", 40: "forty", 45: "forty-five", 50: "fifty",
    60: "sixty", 90: "ninety", 180: "one hundred eighty",
}


def words(n: int) -> str:
    return NUM_WORDS.get(n, str(n))


def money(n: int) -> str:
    return f"US${n:,.0f}"


# --------------------------------------------------------------------------------------------
# Clause renderers: leaf key -> (sub-heading, sentence). Two wording variants each so the
# "clause variants" feature has something to group.
# --------------------------------------------------------------------------------------------
def clause(key: str, v, variant: int = 0) -> tuple[str, str]:
    if key == "base_salary":
        return "Base Salary", [
            f"The Trader shall receive an annual base salary of {money(v)}, payable in accordance with the Firm's regular payroll practices.",
            f"The Firm shall pay the Trader a base salary at the annual rate of {money(v)}.",
        ][variant]
    if key == "payout_pct_net_pnl":
        return "Payout", [
            f"The Trader shall be entitled to a discretionary payout equal to {words(v)} percent ({v}%) of Net Trading P&L for each Performance Year.",
            f"For each Performance Year the Trader's payout shall be {words(v)} percent ({v}%) of Net Trading P&L attributable to the Trader's book.",
        ][variant]
    if key == "hurdle_amount":
        return "Hurdle", [
            f"No payout shall accrue until Net Trading P&L for the Performance Year exceeds a hurdle of {money(v)}.",
            f"Payout is subject to a minimum annual hurdle of {money(v)} in Net Trading P&L.",
        ][variant]
    if key == "hwm_applies":
        return "High-Water Mark", [
            "Payout is subject to a high-water mark: cumulative losses must be recovered before any further payout accrues."
            if v else "No high-water mark shall apply to the Trader's payout.",
            "A high-water mark applies to the Trader's book." if v else "The high-water mark provisions do not apply.",
        ][variant]
    if key == "netting_scope":
        return "Netting", [
            f"Profits and losses shall be netted at the {v} level.",
            f"For the purposes of payout, P&L netting shall be applied at the {v} level.",
        ][variant]
    if key == "deferral_pct":
        return "Deferral", [
            f"{words(v).capitalize()} percent ({v}%) of any payout in excess of the Deferral Threshold shall be deferred.",
            f"The Firm shall defer {words(v)} percent ({v}%) of payout above the Deferral Threshold.",
        ][variant]
    if key == "deferral_threshold":
        return "Deferral Threshold", [
            f"The Deferral Threshold is {money(v)}.",
            f"Deferral applies only to payout amounts above {money(v)} (the \"Deferral Threshold\").",
        ][variant]
    if key == "vesting_years":
        return "Vesting", [
            f"Deferred amounts shall vest over {words(v)} ({v}) years.",
            f"Deferred compensation vests over a period of {words(v)} ({v}) years from the grant date.",
        ][variant]
    if key == "vesting_type":
        return "Vesting Method", [
            f"Vesting shall be on a {v} basis.",
            f"Deferred awards vest on a {v} basis.",
        ][variant]
    if key == "clawback_lookback_years":
        return "Clawback", [
            f"Any payout is subject to clawback by the Firm for a period of {words(v)} ({v}) years following payment in the event of misconduct or material restatement.",
            f"The Firm may recover payouts made within the preceding {words(v)} ({v}) years where misconduct or a material restatement is established.",
        ][variant]
    if key == "guaranteed_min_payout":
        return "Guaranteed Minimum Payout", [
            f"The Firm guarantees the Trader a minimum payout of {money(v)}.",
            f"The Trader's payout shall be no less than {money(v)}.",
        ][variant]
    if key == "draw_amount":
        return "Draw", [
            f"The Trader may receive a recoverable draw of {money(v)} against future payout.",
            f"A recoverable draw of {money(v)} shall be advanced against future payout.",
        ][variant]
    if key == "noncompete_months":
        return "Non-Competition", [
            f"For {words(v)} ({v}) months following termination the Trader shall not engage in a Competing Business.",
            f"The Trader agrees not to work for a Competing Business for a period of {words(v)} ({v}) months after the termination date.",
        ][variant]
    if key == "nonsolicit_months":
        return "Non-Solicitation", [
            f"For {words(v)} ({v}) months following termination the Trader shall not solicit any employee or investor of the Firm.",
            f"The Trader shall not solicit Firm employees or investors for {words(v)} ({v}) months after termination.",
        ][variant]
    if key == "notice_period_days":
        return "Notice Period", [
            f"Either party may terminate employment on {words(v)} ({v}) days' written notice.",
            f"Employment may be terminated by either party giving {words(v)} ({v}) days' written notice.",
        ][variant]
    if key == "garden_leave_days":
        return "Garden Leave", [
            f"The Firm may place the Trader on garden leave for up to {words(v)} ({v}) days of the notice period.",
            f"During the notice period the Firm may require up to {words(v)} ({v}) days of garden leave.",
        ][variant]
    raise KeyError(key)


# leaf key -> (term, sub_term) taxonomy path
TAXO = {
    "base_salary": ("Compensation", "Base"),
    "payout_pct_net_pnl": ("Compensation", "Payout"),
    "hurdle_amount": ("Compensation", "Hurdle"),
    "hwm_applies": ("Compensation", "HighWaterMark"),
    "netting_scope": ("Compensation", "Netting"),
    "deferral_pct": ("Deferral", "Schedule"),
    "deferral_threshold": ("Deferral", "Schedule"),
    "vesting_years": ("Deferral", "Vesting"),
    "vesting_type": ("Deferral", "Vesting"),
    "clawback_lookback_years": ("Clawback", "Terms"),
    "guaranteed_min_payout": ("Guarantee", "Guarantee"),
    "draw_amount": ("Guarantee", "Draw"),
    "noncompete_months": ("RestrictiveCovenants", "NonCompete"),
    "nonsolicit_months": ("RestrictiveCovenants", "NonSolicit"),
    "notice_period_days": ("Termination", "Notice"),
    "garden_leave_days": ("Termination", "Notice"),
}
TERM_HEADINGS = {
    "Compensation": "Compensation", "Deferral": "Deferred Compensation", "Clawback": "Clawback",
    "Guarantee": "Guarantee and Draw", "RestrictiveCovenants": "Restrictive Covenants",
    "Termination": "Termination",
}


@dataclass
class Doc:
    doc_id: str
    trader_id: str
    doc_type: str  # base_agreement | amendment | side_letter | memo
    title: str
    doc_date: str
    fmt: str  # pdf | docx | scan
    values: dict = field(default_factory=dict)  # leaf -> value (set)
    removes: list = field(default_factory=list)  # leaf keys explicitly removed
    scope_note: str | None = None
    text_effective_date: str | None = None
    variant: int = 0


def path_of(key: str) -> str:
    t, s = TAXO[key]
    return f"{t}.{s}.{key}"


# --------------------------------------------------------------------------------------------
# Build document content (sections) from a Doc spec
# --------------------------------------------------------------------------------------------
def build_sections(d: Doc, trader_name: str) -> tuple[str, list[dict]]:
    """Returns preamble + list of sections {number, heading, paras:[...], table: [[...]] | None}."""
    if d.doc_type == "base_agreement":
        pre = (f"This Employment and Compensation Agreement (the \"Agreement\") is dated {d.doc_date} and is made between "
               f"{FIRM} (the \"Firm\") and {trader_name} (Trader ID {d.trader_id}) (the \"Trader\").")
    elif d.doc_type == "amendment":
        pre = (f"This {d.title} is dated {d.doc_date} and amends the Employment and Compensation Agreement between "
               f"{FIRM} and {trader_name} (Trader ID {d.trader_id}). Except as set out below, the Agreement remains in full force and effect.")
    elif d.doc_type == "side_letter":
        pre = (f"This side letter dated {d.doc_date} is delivered by {FIRM} to {trader_name} (Trader ID {d.trader_id}) "
               f"and supplements the Agreement.")
    else:
        pre = (f"Memorandum dated {d.doc_date} from Compensation Committee, {FIRM}, regarding {trader_name} (Trader ID {d.trader_id}).")
    if d.text_effective_date:
        pre += f" The changes in this document are effective {d.text_effective_date}."
    if d.scope_note:
        pre += f" {d.scope_note}"

    sections: list[dict] = [{"number": "1", "heading": "Definitions", "paras": [
        "\"Net Trading P&L\" means the realised and unrealised trading profits and losses of the Trader's book, net of direct costs and allocated financing charges.",
        "\"Performance Year\" means each calendar year ending 31 December.",
    ], "table": None}]
    # group leaves by term
    by_term: dict[str, list[str]] = {}
    for k in list(d.values) + d.removes:
        by_term.setdefault(TAXO[k][0], []).append(k)
    order = ["Compensation", "Deferral", "Clawback", "Guarantee", "RestrictiveCovenants", "Termination"]
    n = 2
    for term in order:
        if term not in by_term:
            continue
        sec = {"number": str(n), "heading": TERM_HEADINGS[term], "paras": [], "table": None, "subs": []}
        for i, k in enumerate(by_term[term], start=1):
            if k in d.removes:
                head, _ = clause(k, 1)
                sec["subs"].append({"number": f"{n}.{i}", "heading": head,
                                    "paras": [f"The {head} provisions of the Agreement are deleted in their entirety and shall have no further effect."]})
                continue
            head, sent = clause(k, d.values[k], d.variant)
            if d.doc_type == "amendment":
                sent = f"The {head} provisions of the Agreement are deleted and replaced with the following: {sent}"
            sec["subs"].append({"number": f"{n}.{i}", "heading": head, "paras": [sent]})
        if term == "Deferral" and "vesting_years" in d.values and d.values.get("vesting_type") == "pro-rata":
            yrs = d.values["vesting_years"]
            share = round(100 / yrs, 1)
            sec["table"] = [["Anniversary", "Cumulative Vested %"]] + [
                [f"Year {i}", f"{min(100, round(share * i, 1))}%"] for i in range(1, yrs + 1)]
        sections.append(sec)
        n += 1
    sections.append({"number": str(n), "heading": "Governing Law", "paras": [
        "This document is governed by the laws of the State of New York."], "table": None})
    return pre, sections


# --------------------------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------------------------
def render_pdf(path: Path, title: str, pre: str, sections: list[dict]) -> None:
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontSize=15)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12.5)
    h3 = ParagraphStyle("h3", parent=ss["Heading3"], fontSize=11)
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=10, leading=13)
    esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;")  # noqa: E731  reportlab parses markup
    story = [Paragraph(esc(title), h1), Paragraph(esc(pre), body), Spacer(1, 8)]
    for s in sections:
        story.append(Paragraph(esc(f"{s['number']}. {s['heading']}"), h2))
        for p in s.get("paras", []):
            story.append(Paragraph(esc(p), body))
        for sub in s.get("subs", []):
            story.append(Paragraph(esc(f"{sub['number']} {sub['heading']}"), h3))
            for p in sub["paras"]:
                story.append(Paragraph(esc(p), body))
        if s.get("table"):
            t = Table(s["table"], colWidths=[2 * inch, 2 * inch])
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, "#444444"),
                                   ("BACKGROUND", (0, 0), (-1, 0), "#DDDDDD")]))
            story += [Spacer(1, 6), t, Spacer(1, 6)]
    story += [Spacer(1, 18), Paragraph("CONFIDENTIAL - FOR INTERNAL USE ONLY", body),
              Paragraph(f"Signed for and on behalf of {FIRM}: ____________________", body)]
    SimpleDocTemplate(str(path), pagesize=LETTER, title=title).build(story)


def render_docx(path: Path, title: str, pre: str, sections: list[dict]) -> None:
    doc = DocxDocument()
    doc.add_heading(title, level=0)
    doc.add_paragraph(pre)
    for s in sections:
        doc.add_heading(f"{s['number']}. {s['heading']}", level=1)
        for p in s.get("paras", []):
            doc.add_paragraph(p)
        for sub in s.get("subs", []):
            doc.add_heading(f"{sub['number']} {sub['heading']}", level=2)
            for p in sub["paras"]:
                doc.add_paragraph(p)
        if s.get("table"):
            rows = s["table"]
            t = doc.add_table(rows=len(rows), cols=len(rows[0]))
            t.style = "Table Grid"
            for i, r in enumerate(rows):
                for j, c in enumerate(r):
                    t.cell(i, j).text = c
    doc.add_paragraph("CONFIDENTIAL - FOR INTERNAL USE ONLY")
    doc.save(str(path))


def render_scan(path: Path, title: str, pre: str, sections: list[dict]) -> None:
    """Render a PDF, rasterise each page and rebuild an image-only PDF (no text layer)."""
    import pypdfium2 as pdfium
    tmp = path.with_suffix(".tmp.pdf")
    render_pdf(tmp, title, pre, sections)
    pdf = pdfium.PdfDocument(str(tmp))
    images = [pdf[i].render(scale=2.2).to_pil().convert("L").convert("RGB") for i in range(len(pdf))]
    pdf.close()
    images[0].save(str(path), "PDF", resolution=160, save_all=True, append_images=images[1:])
    tmp.unlink()


# --------------------------------------------------------------------------------------------
# Trader scenarios
# --------------------------------------------------------------------------------------------
NAMES = ["Alex Morgan", "Priya Raman", "Daniel Okafor", "Sofia Lindqvist", "Kenji Watanabe",
         "Laura Benedetti", "Marcus Hale", "Elena Petrova", "Omar Siddiqui", "Grace Whitfield"]


def base_values(rng: random.Random) -> dict:
    return {
        "base_salary": rng.choice([200000, 225000, 250000, 300000]),
        "payout_pct_net_pnl": rng.choice([12, 14, 15, 16]),
        "hurdle_amount": rng.choice([1000000, 2000000, 2500000]),
        "hwm_applies": True,
        "netting_scope": rng.choice(["desk", "individual book"]),
        "deferral_pct": rng.choice([25, 30, 40]),
        "deferral_threshold": rng.choice([1000000, 1500000]),
        "vesting_years": 3,
        "vesting_type": rng.choice(["pro-rata", "cliff"]),
        "noncompete_months": rng.choice([6, 9, 12]),
        "nonsolicit_months": 12,
        "notice_period_days": rng.choice([60, 90]),
    }


def scenarios() -> tuple[list[Doc], dict[str, str]]:
    rng = random.Random(7)
    docs: list[Doc] = []
    names: dict[str, str] = {}
    did = iter(range(1, 999))

    def nid() -> str:
        return f"D-{next(did):03d}"

    for i, name in enumerate(NAMES):
        tid = f"T-{1001 + i}"
        names[tid] = name
        y = 2021 + (i % 3)
        bv = base_values(rng)
        fmt = "docx" if i % 2 else "pdf"
        docs.append(Doc(nid(), tid, "base_agreement", "Employment and Compensation Agreement", f"{y}-01-15", fmt, bv))

        if tid == "T-1003":  # classic amendment chain: payout 15 -> 17, deferral change, later clawback added
            docs[-1].values["payout_pct_net_pnl"] = 15
            docs.append(Doc(nid(), tid, "amendment", "Amendment No. 1", "2024-02-01", "pdf",
                            {"deferral_pct": 35, "vesting_years": 4}, variant=1))
            docs.append(Doc(nid(), tid, "amendment", "Amendment No. 2", "2025-06-01", "docx",
                            {"payout_pct_net_pnl": 17, "clawback_lookback_years": 3}))
            docs.append(Doc(nid(), tid, "memo", "Notice Period Memo", "2025-09-10", "pdf",
                            {"notice_period_days": 90, "garden_leave_days": 60}))
        elif tid == "T-1004":  # same-date conflict
            docs.append(Doc(nid(), tid, "amendment", "Amendment No. 1", "2024-07-01", "pdf", {"payout_pct_net_pnl": 18}))
            docs.append(Doc(nid(), tid, "side_letter", "Side Letter", "2024-07-01", "docx", {"payout_pct_net_pnl": 19}, variant=1))
        elif tid == "T-1005":  # scoped override (FY2025 only) + guarantee
            docs.append(Doc(nid(), tid, "side_letter", "Side Letter - FY2025 Guarantee", "2025-01-10", "pdf",
                            {"guaranteed_min_payout": 1500000, "payout_pct_net_pnl": 20},
                            scope_note="The terms of this side letter apply for the Performance Year 2025 only."))
        elif tid == "T-1006":  # scanned amendment
            docs.append(Doc(nid(), tid, "amendment", "Amendment No. 1", "2024-03-15", "scan",
                            {"payout_pct_net_pnl": 18, "noncompete_months": 12}))
        elif tid == "T-1007":  # explicit removal of non-compete
            docs.append(Doc(nid(), tid, "amendment", "Amendment No. 1", "2024-11-01", "docx",
                            {"nonsolicit_months": 6}, removes=["noncompete_months"]))
        elif tid == "T-1008":  # effective date in text differs from DB date (signed late, effective earlier)
            docs.append(Doc(nid(), tid, "amendment", "Amendment No. 1", "2025-03-20", "pdf",
                            {"base_salary": 350000, "draw_amount": 500000}, text_effective_date="2025-01-01"))
            docs.append(Doc(nid(), tid, "memo", "Salary Memo", "2025-02-01", "docx", {"base_salary": 325000}))
        elif tid == "T-1009":  # spread across many docs, none an "amendment" of the same term
            docs.append(Doc(nid(), tid, "side_letter", "Side Letter - Draw", "2023-05-01", "docx", {"draw_amount": 400000}))
            docs.append(Doc(nid(), tid, "memo", "Clawback Policy Memo", "2024-01-05", "pdf", {"clawback_lookback_years": 5}, variant=1))
            docs.append(Doc(nid(), tid, "amendment", "Amendment No. 1", "2025-04-01", "pdf", {"hurdle_amount": 3000000}, variant=1))
        else:  # simple: one amendment changing payout
            docs.append(Doc(nid(), tid, "amendment", "Amendment No. 1", f"{y + 2}-04-01", "docx" if i % 2 == 0 else "pdf",
                            {"payout_pct_net_pnl": bv["payout_pct_net_pnl"] + 2}, variant=i % 2))
    return docs, names


def gold_for(tid: str, docs: list[Doc]) -> dict:
    """Expected current values after reconciliation (latest effective date wins; omission keeps old)."""
    mine = [d for d in docs if d.trader_id == tid]
    eff = lambda d: d.text_effective_date or d.doc_date  # noqa: E731
    current: dict[str, dict] = {}
    conflicts: list[str] = []
    for d in sorted(mine, key=eff):
        for k, v in d.values.items():
            p = path_of(k)
            prev = current.get(p)
            if prev and prev["effective"] == eff(d) and prev["value"] != v:
                conflicts.append(p)
            if d.scope_note:
                conflicts.append(p)  # scoped override must be flagged, not silently applied
            current[p] = {"value": v, "doc_id": d.doc_id, "effective": eff(d)}
        for k in d.removes:
            current[path_of(k)] = {"value": None, "doc_id": d.doc_id, "effective": eff(d), "removed": True}
    return {"trader_id": tid, "current": current, "expected_conflicts": sorted(set(conflicts)),
            "findings": [{"doc_id": d.doc_id, "path": path_of(k), "value": v} for d in mine for k, v in d.values.items()]}


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "docs").mkdir(parents=True)
    (OUT / "gold").mkdir()
    docs, names = scenarios()
    manifest = []
    for d in docs:
        pre, sections = build_sections(d, names[d.trader_id])
        ext = "docx" if d.fmt == "docx" else "pdf"
        path = OUT / "docs" / f"{d.doc_id}.{ext}"
        title = f"{d.title} - {names[d.trader_id]}"
        {"pdf": render_pdf, "docx": render_docx, "scan": render_scan}[d.fmt](path, title, pre, sections)
        manifest.append({"doc_id": d.doc_id, "trader_id": d.trader_id, "trader_name": names[d.trader_id],
                         "title": d.title, "doc_type": d.doc_type, "doc_date": d.doc_date,
                         "path": str(path.relative_to(OUT)), "format": d.fmt})
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    for tid in names:
        (OUT / "gold" / f"{tid}.json").write_text(json.dumps(gold_for(tid, docs), indent=2, default=str))
    print(f"Wrote {len(docs)} documents for {len(names)} traders to {OUT}")


if __name__ == "__main__":
    main()
