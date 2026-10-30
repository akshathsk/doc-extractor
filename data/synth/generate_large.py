"""Generate LARGE, realistic synthetic contracts for scale and structure testing.

Each base agreement is ~200 pages with what real contracts have and the small set lacks:
  cover page, table of contents, recitals (WHEREAS / NOW, THEREFORE), ARTICLE I..XV in Roman numerals,
  "Section 7.02 Payout." run-in headings, (a)/(i) clauses, long boilerplate articles, comp terms buried deep
  (sometimes paraphrased), Schedules and Exhibits with tables, running header/footer on every page,
  a signature page, and a scanned (image-only) signature page.
Plus amendments (DOCX + PDF), a fully scanned side letter and comp memos, so one trader can have up to 30 docs.

Output: data/synth/out_large/
  docs/*.pdf|docx, manifest.json (same shape as the small set), gold/<trader>.json (values),
  structure/<doc_id>.json (expected section tree: id, style, number, heading, parent, level)

Usage: uv run python data/synth/generate_large.py [--traders 2] [--docs 6] [--pages 200]
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document as DocxDocument
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path(__file__).parent / "out_large"
FIRM = "Meridian Ridge Capital LP"
ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII",
         "XVIII", "XIX", "XX"]
LOWER_ROMAN = ["", "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"]
WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 9: "nine", 12: "twelve", 14: "fourteen", 15: "fifteen",
         16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen", 20: "twenty", 25: "twenty-five", 30: "thirty",
         35: "thirty-five", 40: "forty", 60: "sixty", 90: "ninety"}

# ------------------------------------------------------------------------------------------ boilerplate
BOILER = [
    "The Trader shall perform such duties as are customarily associated with the position and as the Firm may reasonably assign from time to time.",
    "Nothing in this Agreement shall be construed to create a partnership, joint venture or agency relationship between the parties.",
    "The Trader shall comply with all written policies of the Firm, including its compliance manual, code of ethics and personal trading policy, as amended from time to time.",
    "Any notice required under this Agreement shall be in writing and delivered by hand, by courier or by electronic mail to the addresses set out herein.",
    "The failure of either party to enforce any provision of this Agreement shall not be construed as a waiver of that provision or of the right to enforce it thereafter.",
    "If any provision of this Agreement is held invalid or unenforceable, the remaining provisions shall continue in full force and effect.",
    "The Trader acknowledges that the Firm's business is highly competitive and that its methods, strategies and positions are of substantial value.",
    "The Trader shall not, during or after employment, use or disclose any Confidential Information except as required in the proper performance of duties.",
    "All work product, models, code and analyses created by the Trader in the course of employment shall be the exclusive property of the Firm.",
    "The parties shall attempt in good faith to resolve any dispute arising under this Agreement through negotiation before commencing arbitration.",
    "This Agreement may be executed in counterparts, each of which shall be deemed an original and all of which together constitute one instrument.",
    "The Firm may withhold from any amount payable under this Agreement such taxes as are required to be withheld under applicable law.",
    "The Trader shall promptly disclose to the Firm any actual or potential conflict of interest of which the Trader becomes aware.",
    "Headings in this Agreement are for convenience only and shall not affect its interpretation.",
    "The Trader shall maintain all licenses and registrations required by applicable regulators for the performance of the Trader's duties.",
    "Except as expressly provided herein, no amendment to this Agreement shall be effective unless made in writing and signed by both parties.",
    "The Trader shall cooperate fully with any internal or regulatory investigation relating to matters within the Trader's knowledge.",
    "The Firm shall reimburse the Trader for reasonable business expenses incurred in accordance with the Firm's expense policy.",
]
FILLER_ARTICLES = ["Definitions and Interpretation", "Term of Employment", "Duties and Responsibilities", "Compliance and Regulatory Matters",
                   "Confidential Information", "Intellectual Property", "Conflicts of Interest", "Expenses and Benefits",
                   "Representations and Warranties", "Indemnification", "Dispute Resolution", "Notices", "Miscellaneous"]
FILLER_SECTION_NAMES = ["General", "Scope", "Standards", "Obligations", "Exceptions", "Procedures", "Records", "Cooperation",
                        "Remedies", "Survival", "Reporting", "Limitations"]


def words(n: int) -> str:
    return WORDS.get(n, str(n))


def money(n: int) -> str:
    return f"US${n:,.0f}"


# ------------------------------------------------------------------------------------------ comp clauses (two wordings each)
def comp_clause(key: str, v, para: bool) -> tuple[str, str]:
    """(heading, sentence). para=True uses paraphrased wording (for semantic-search tests)."""
    if key == "base_salary":
        return ("Base Salary", f"The Firm shall pay the Trader an annual base salary of {money(v)}, payable in arrears in equal instalments." if not para
                else f"The Trader's fixed annual remuneration shall be {money(v)}, paid through the Firm's regular payroll.")
    if key == "payout_pct_net_pnl":
        return ("Payout", f"The Trader shall be entitled to a discretionary payout equal to {words(v)} percent ({v}%) of Net Trading P&L for each Performance Year." if not para
                else f"The Trader shall be eligible for incentive compensation equal to {words(v)} percent ({v}%) of the Net Trading Revenue generated by the Trader's book in each Performance Year.")
    if key == "hurdle_amount":
        return ("Hurdle", f"No payout shall accrue until Net Trading P&L for the Performance Year exceeds a hurdle of {money(v)}." if not para
                else f"Incentive compensation is earned only on Net Trading Revenue above a threshold of {money(v)} in the Performance Year.")
    if key == "deferral_pct":
        return ("Deferral", f"{words(v).capitalize()} percent ({v}%) of any payout in excess of the Deferral Threshold shall be deferred." if not para
                else f"The Firm shall withhold and defer {words(v)} percent ({v}%) of incentive compensation above the Deferral Threshold.")
    if key == "vesting_years":
        return ("Vesting", f"Deferred amounts shall vest over {words(v)} ({v}) years." if not para
                else f"Deferred awards become non-forfeitable over a period of {words(v)} ({v}) years from the grant date.")
    if key == "clawback_lookback_years":
        return ("Clawback", f"Any payout is subject to clawback by the Firm for a period of {words(v)} ({v}) years following payment in the event of misconduct or material restatement." if not para
                else f"The Firm may recoup any incentive compensation paid within the preceding {words(v)} ({v}) years where misconduct or a material restatement is established.")
    if key == "noncompete_months":
        return ("Non-Competition", f"For {words(v)} ({v}) months following termination the Trader shall not engage in a Competing Business." if not para
                else f"During the Restricted Period of {words(v)} ({v}) months after the termination date the Trader shall not be employed by or provide services to a Competing Business.")
    if key == "notice_period_days":
        return ("Notice Period", f"Either party may terminate employment on {words(v)} ({v}) days' written notice." if not para
                else f"Employment may be ended by either party upon {words(v)} ({v}) days' prior written notice.")
    raise KeyError(key)


PATHS = {"base_salary": "Compensation.Base.base_salary", "payout_pct_net_pnl": "Compensation.Payout.payout_pct_net_pnl",
         "hurdle_amount": "Compensation.Hurdle.hurdle_amount", "deferral_pct": "Deferral.Schedule.deferral_pct",
         "vesting_years": "Deferral.Vesting.vesting_years", "clawback_lookback_years": "Clawback.Terms.clawback_lookback_years",
         "noncompete_months": "RestrictiveCovenants.NonCompete.noncompete_months",
         "notice_period_days": "Termination.Notice.notice_period_days"}
COMP_ARTICLE = {"base_salary": "Compensation", "payout_pct_net_pnl": "Compensation", "hurdle_amount": "Compensation",
                "deferral_pct": "Deferred Compensation", "vesting_years": "Deferred Compensation",
                "clawback_lookback_years": "Clawback and Recoupment", "noncompete_months": "Restrictive Covenants",
                "notice_period_days": "Termination"}


# ------------------------------------------------------------------------------------------ document model
@dataclass
class Node:
    style: str  # article | section | clause_alpha | clause_roman | attachment | recitals | signature
    label: str  # as printed: "ARTICLE IV", "Section 4.02", "(a)", "(i)", "Schedule A"
    norm: str   # expected normalised number: "art4", "4.2", "a", "i", "schA"
    heading: str
    paras: list[str] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)
    table: list[list[str]] | None = None
    run_in: bool = False  # "Section 7.02 Payout. The Trader shall ..." on one line


@dataclass
class DocSpec:
    doc_id: str
    trader_id: str
    trader_name: str
    title: str
    doc_type: str
    doc_date: str
    fmt: str  # pdf | docx | scan
    nodes: list[Node]
    values: dict  # leaf -> value set by this doc
    pages_target: int = 0
    toc: bool = False
    scanned_signature: bool = False


def boiler(rng: random.Random, n: int) -> list[str]:
    return [" ".join(rng.sample(BOILER, k=rng.randint(3, 5))) for _ in range(n)]


def build_base_agreement(rng: random.Random, doc_id: str, tid: str, name: str, date: str, values: dict, pages: int,
                         para: bool) -> DocSpec:
    """Articles in Roman numerals; comp terms in a deep article; long boilerplate to reach `pages`."""
    words_target = pages * 560
    comp_keys = list(values)
    articles: list[str] = []
    fill = list(FILLER_ARTICLES)
    # comp articles land roughly two thirds through the document
    comp_articles = []
    for k in comp_keys:
        a = COMP_ARTICLE[k]
        if a not in comp_articles:
            comp_articles.append(a)
    insert_at = max(3, int(len(fill) * 0.6))
    articles = fill[:insert_at] + comp_articles + fill[insert_at:]
    nodes: list[Node] = [Node("recitals", "RECITALS", "recitals", "Recitals", paras=[
        f"WHEREAS, the Firm wishes to employ {name} (Trader ID {tid}) as a portfolio manager on the terms set out herein;",
        "WHEREAS, the Trader wishes to accept such employment on such terms;",
        "NOW, THEREFORE, in consideration of the mutual covenants herein, the parties agree as follows:"])]
    per_article_words = words_target // len(articles)
    for ai, aname in enumerate(articles, start=1):
        art = Node("article", f"ARTICLE {ROMAN[ai]}", f"art{ai}", aname.upper())
        if aname in comp_articles:
            keys = [k for k in comp_keys if COMP_ARTICLE[k] == aname]
            for si, k in enumerate(keys, start=1):
                head, sent = comp_clause(k, values[k], para and rng.random() < 0.6)
                sec = Node("section", f"Section {ai}.{si:02d}", f"{ai}.{si}", head, run_in=True, paras=[sent])
                # (a)/(i) clauses with conditions around the economic term
                sec.children = [
                    Node("clause_alpha", "(a)", "a", "", paras=["the amount shall be determined in accordance with the Firm's compensation policies;"],
                         children=[Node("clause_roman", "(i)", "i", "", paras=["subject to the Trader's continued employment on the payment date;"]),
                                   Node("clause_roman", "(ii)", "ii", "", paras=["subject to the Trader not having given or received notice of termination;"])]),
                    Node("clause_alpha", "(b)", "b", "", paras=["the Firm's determination shall be final and binding absent manifest error."]),
                ]
                art.children.append(sec)
        n_sections = rng.randint(4, 9)
        start = len(art.children) + 1
        target_words_here = per_article_words
        for si in range(start, start + n_sections):
            sname = rng.choice(FILLER_SECTION_NAMES)
            n_paras = max(1, target_words_here // n_sections // 80)
            sec = Node("section", f"Section {ai}.{si:02d}", f"{ai}.{si}", sname, run_in=rng.random() < 0.7, paras=boiler(rng, n_paras))
            if rng.random() < 0.35:
                sec.children = [Node("clause_alpha", f"({c})", c, "", paras=boiler(rng, 1)) for c in "abc"[: rng.randint(2, 3)]]
            art.children.append(sec)
        nodes.append(art)
    # schedules and exhibits
    sch_a = Node("attachment", "Schedule A", "schA", "Payout Grid", paras=["The following grid illustrates the payout under the Agreement."],
                 table=[["Net Trading P&L (US$m)", "Payout rate"]] + [[f"{lo}-{lo + 10}", f"{values.get('payout_pct_net_pnl', 15)}%"] for lo in range(0, 50, 10)])
    sch_b = Node("attachment", "Schedule B", "schB", "Vesting Schedule", paras=["Deferred amounts vest as follows."],
                 table=[["Anniversary", "Cumulative vested %"]] + [[f"Year {y}", f"{round(100 * y / values.get('vesting_years', 3), 1)}%"]
                                                                  for y in range(1, values.get("vesting_years", 3) + 1)])
    exh_a = Node("attachment", "Exhibit A", "exhA", "Form of Release", paras=boiler(rng, 6))
    nodes += [sch_a, sch_b, exh_a, Node("signature", "SIGNATURES", "signature", "Signature Page",
                                        paras=[f"IN WITNESS WHEREOF the parties have executed this Agreement as of {date}.",
                                               f"For and on behalf of {FIRM}: ____________________", f"{name}: ____________________"])]
    return DocSpec(doc_id, tid, name, "Employment and Compensation Agreement", "base_agreement", date, "pdf", nodes, dict(values),
                   pages_target=pages, toc=True, scanned_signature=True)


def build_amendment(rng: random.Random, doc_id: str, tid: str, name: str, date: str, values: dict, n: int, fmt: str, para: bool,
                    filler_pages: int = 3) -> DocSpec:
    nodes = [Node("recitals", "RECITALS", "recitals", "Recitals", paras=[
        f"WHEREAS, the Firm and {name} (Trader ID {tid}) are parties to an Employment and Compensation Agreement (the \"Agreement\");",
        "NOW, THEREFORE, the parties agree to amend the Agreement as follows:"])]
    art = Node("article", "ARTICLE I", "art1", "AMENDMENTS")
    for si, (k, v) in enumerate(values.items(), start=1):
        head, sent = comp_clause(k, v, para)
        art.children.append(Node("section", f"Section 1.{si:02d}", f"1.{si}", head, run_in=True,
                                 paras=[f"The {head} provisions of the Agreement are deleted and replaced with the following: {sent}"]))
    nodes.append(art)
    gen = Node("article", "ARTICLE II", "art2", "GENERAL")
    for si in range(1, 2 + filler_pages * 2):
        gen.children.append(Node("section", f"Section 2.{si:02d}", f"2.{si}", rng.choice(FILLER_SECTION_NAMES), run_in=True, paras=boiler(rng, 3)))
    nodes.append(gen)
    nodes.append(Node("signature", "SIGNATURES", "signature", "Signature Page", paras=[f"Executed as of {date}.", "____________________"]))
    return DocSpec(doc_id, tid, name, f"Amendment No. {n}", "amendment", date, fmt, nodes, dict(values))


# ------------------------------------------------------------------------------------------ structure gold
def structure_gold(spec: DocSpec) -> list[dict]:
    out = []

    def sid(path: list[str]) -> str:
        return f"{spec.doc_id}:s" + ".".join(path)

    def walk(n: Node, parent_path: list[str] | None, level: int):
        if n.style in ("article", "attachment", "recitals", "signature"):
            path = [n.norm]
        elif n.style == "section":
            path = [n.norm]
        else:
            path = (parent_path or []) + [n.norm]
        out.append({"id": sid(path), "style": n.style, "label": n.label, "number": n.norm, "heading": n.heading,
                    "parent": sid(parent_path) if parent_path else None, "level": level})
        for c in n.children:
            walk(c, path, level + 1)
    for n in spec.nodes:
        walk(n, None, 1)
    return out


# ------------------------------------------------------------------------------------------ renderers
def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;")


def render_pdf(spec: DocSpec, path: Path) -> None:
    ss = getSampleStyleSheet()
    title = ParagraphStyle("t", parent=ss["Title"], fontSize=18)
    art_s = ParagraphStyle("a", parent=ss["Heading1"], fontSize=13.5, spaceBefore=14)
    sec_s = ParagraphStyle("s", parent=ss["Heading2"], fontSize=11.5)
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=10, leading=13)
    clause = ParagraphStyle("c", parent=body, leftIndent=24)
    clause2 = ParagraphStyle("c2", parent=body, leftIndent=48)
    toc_s = ParagraphStyle("toc", parent=body, fontSize=9.5)
    story = [Spacer(1, 2 * inch), Paragraph(_esc(f"{spec.title.upper()}"), title),
             Paragraph(_esc(f"between {FIRM} and {spec.trader_name} (Trader ID {spec.trader_id})"), body),
             Paragraph(_esc(f"Dated {spec.doc_date}"), body), PageBreak()]
    if spec.toc:
        story.append(Paragraph("TABLE OF CONTENTS", art_s))
        pg = 3
        for n in spec.nodes:
            if n.style in ("article", "attachment"):
                story.append(Paragraph(_esc(f"{n.label} {n.heading} " + "." * 40 + f" {pg}"), toc_s))
                pg += max(1, spec.pages_target // max(1, len(spec.nodes)))
        story.append(PageBreak())

    def add_node(n: Node, depth: int):
        if n.style in ("article", "attachment"):
            if n.style == "attachment":
                story.append(PageBreak())
            story.append(Paragraph(_esc(f"{n.label} - {n.heading}" if n.style == "attachment" else f"{n.label} - {n.heading}"), art_s))
            for p in n.paras:
                story.append(Paragraph(_esc(p), body))
        elif n.style in ("recitals", "signature"):
            if n.style == "signature":
                story.append(PageBreak())
            story.append(Paragraph(_esc(n.heading.upper()), art_s))
            for p in n.paras:
                story.append(Paragraph(_esc(p), body))
        elif n.style == "section":
            if n.run_in:
                first = n.paras[0] if n.paras else ""
                story.append(Paragraph(f"<b>{_esc(n.label)} {_esc(n.heading)}.</b> {_esc(first)}", body))
                rest = n.paras[1:]
            else:
                story.append(Paragraph(_esc(f"{n.label} {n.heading}"), sec_s))
                rest = n.paras
            for p in rest:
                story.append(Paragraph(_esc(p), body))
        else:
            st = clause if n.style == "clause_alpha" else clause2
            story.append(Paragraph(_esc(f"{n.label} {' '.join(n.paras)}"), st))
        if n.table:
            t = Table(n.table, colWidths=[2.4 * inch, 2 * inch])
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, "#444444"), ("BACKGROUND", (0, 0), (-1, 0), "#DDDDDD")]))
            story.extend([Spacer(1, 6), t, Spacer(1, 6)])
        for c in n.children:
            add_node(c, depth + 1)
    for n in spec.nodes:
        add_node(n, 0)

    header = f"{FIRM} - {spec.title} - CONFIDENTIAL"

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawString(inch, LETTER[1] - 0.6 * inch, header)
        canvas.drawRightString(LETTER[0] - inch, 0.55 * inch, f"Page {doc.page}")
        canvas.restoreState()
    tmp = path.with_suffix(".tmp.pdf")
    SimpleDocTemplate(str(tmp), pagesize=LETTER, title=spec.title).build(story, onFirstPage=on_page, onLaterPages=on_page)
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(tmp))
    if spec.fmt == "scan" or spec.scanned_signature:
        # rasterise all pages (scan) or only the last page (scanned signature page) into image-only pages
        out = pdfium.PdfDocument.new()
        last = len(pdf) - 1
        for i in range(len(pdf)):
            if spec.fmt == "scan" or i == last:
                # image-only page: render, save as a one-page PDF, import it (no text layer = needs OCR)
                img = pdf[i].render(scale=2.0).to_pil().convert("L").convert("RGB")
                ipath = path.with_suffix(f".p{i}.pdf")
                img.save(ipath, "PDF", resolution=144)
                one = pdfium.PdfDocument(str(ipath))
                out.import_pages(one)
                one.close()
                ipath.unlink()
            else:
                out.import_pages(pdf, [i])
        out.save(str(path))
        out.close()
    else:
        shutil.copy(tmp, path)
    pdf.close()
    tmp.unlink()


def render_docx(spec: DocSpec, path: Path) -> None:
    d = DocxDocument()
    d.add_heading(spec.title.upper(), level=0)
    d.add_paragraph(f"between {FIRM} and {spec.trader_name} (Trader ID {spec.trader_id}), dated {spec.doc_date}")

    def add(n: Node):
        if n.style in ("article", "attachment"):
            d.add_heading(f"{n.label} - {n.heading}", level=1)
            for p in n.paras:
                d.add_paragraph(p)
        elif n.style in ("recitals", "signature"):
            d.add_heading(n.heading.upper(), level=1)
            for p in n.paras:
                d.add_paragraph(p)
        elif n.style == "section":
            if n.run_in:
                para = d.add_paragraph()
                para.add_run(f"{n.label} {n.heading}. ").bold = True
                para.add_run(n.paras[0] if n.paras else "")
                for p in n.paras[1:]:
                    d.add_paragraph(p)
            else:
                d.add_heading(f"{n.label} {n.heading}", level=2)
                for p in n.paras:
                    d.add_paragraph(p)
        else:
            d.add_paragraph(f"{n.label} {' '.join(n.paras)}")
        if n.table:
            t = d.add_table(rows=len(n.table), cols=len(n.table[0]))
            t.style = "Table Grid"
            for i, r in enumerate(n.table):
                for jx, c in enumerate(r):
                    t.cell(i, jx).text = c
        for c in n.children:
            add(c)
    for n in spec.nodes:
        add(n)
    d.save(str(path))


# ------------------------------------------------------------------------------------------ scenarios
NAMES = ["Victoria Ashdown", "Rahul Menon", "Camille Fournier", "Jonas Berg", "Mei Tanaka"]


def scenario(rng: random.Random, t_idx: int, n_docs: int, pages: int) -> tuple[list[DocSpec], dict]:
    tid, name = f"T-{2001 + t_idx}", NAMES[t_idx % len(NAMES)]
    base_vals = {"base_salary": 300000, "payout_pct_net_pnl": 15, "hurdle_amount": 2000000, "deferral_pct": 30,
                 "vesting_years": 3, "clawback_lookback_years": 3, "noncompete_months": 9, "notice_period_days": 60}
    did = iter(range(1, 999))
    nid = lambda: f"L{t_idx + 1}-{next(did):03d}"  # noqa: E731
    docs = [build_base_agreement(rng, nid(), tid, name, "2021-01-15", base_vals, pages, para=(t_idx % 2 == 1))]
    # yearly amendments and letters; terms change over time
    changes = [("payout_pct_net_pnl", 16), ("deferral_pct", 35), ("vesting_years", 4), ("payout_pct_net_pnl", 17),
               ("noncompete_months", 12), ("notice_period_days", 90), ("hurdle_amount", 2500000), ("clawback_lookback_years", 5)]
    year, month = 2021, 6
    for i in range(1, n_docs):
        k, v = changes[(i - 1) % len(changes)]
        v = v if i <= len(changes) else v + (i // len(changes))
        month += 5
        if month > 12:
            year, month = year + 1, month - 12
        date = f"{year}-{month:02d}-01"
        fmt = ["docx", "pdf", "scan"][i % 3] if i % 7 else "scan"
        docs.append(build_amendment(rng, nid(), tid, name, date, {k: v}, i, fmt, para=(i % 2 == 0), filler_pages=rng.randint(1, 6)))
    # gold: latest date wins (no conflicts in the large set; conflicts are covered by the small set)
    cur: dict = {}
    for d in sorted(docs, key=lambda d: d.doc_date):
        for k, v in d.values.items():
            cur[PATHS[k]] = {"value": v, "doc_id": d.doc_id, "effective": d.doc_date}
    findings = [{"doc_id": d.doc_id, "path": PATHS[k], "value": v} for d in docs for k, v in d.values.items()]
    return docs, {"trader_id": tid, "current": cur, "expected_conflicts": [], "findings": findings}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--traders", type=int, default=2)
    ap.add_argument("--docs", type=int, default=6, help="documents per trader (1 base agreement + amendments)")
    ap.add_argument("--pages", type=int, default=200, help="pages of each base agreement")
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    if OUT.exists():
        shutil.rmtree(OUT)
    for sub in ("docs", "gold", "structure"):
        (OUT / sub).mkdir(parents=True)
    manifest = []
    for t in range(args.traders):
        docs, gold = scenario(rng, t, args.docs, args.pages)
        (OUT / "gold" / f"{gold['trader_id']}.json").write_text(json.dumps(gold, indent=2))
        for d in docs:
            ext = "docx" if d.fmt == "docx" else "pdf"
            path = OUT / "docs" / f"{d.doc_id}.{ext}"
            (render_docx if d.fmt == "docx" else render_pdf)(d, path)
            (OUT / "structure" / f"{d.doc_id}.json").write_text(json.dumps(structure_gold(d), indent=2))
            manifest.append({"doc_id": d.doc_id, "trader_id": d.trader_id, "trader_name": d.trader_name, "title": d.title,
                             "doc_type": d.doc_type, "doc_date": d.doc_date, "path": str(path.relative_to(OUT)), "format": d.fmt})
            print(f"  {d.doc_id} {d.fmt:4} {d.title}")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(manifest)} documents for {args.traders} traders to {OUT}")


if __name__ == "__main__":
    main()
