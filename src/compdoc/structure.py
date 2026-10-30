"""Section builder for real contracts (scaling plan, Part 5).

Turns Docling's flat item list into a clause tree:
  1. clean      drop running headers/footers (by frequency across pages), table-of-contents pages, boilerplate
  2. label      recognise ARTICLE IV / Section 4.02 / 4.2.1 / (a) / (i) / (A) / (1) / Schedule A / Exhibit B / WHEREAS ...
  3. run-in     split "Section 4.02 Payout. The Trader shall ..." into label, heading and body
  4. stack      nest by rank: article < decimal depth < clause styles (in the order they first appear)
  5. letter vs roman   "(i)" after "(h)" is a letter; "(v)" after "(iv)" is roman
  6. validate   sibling order (4.01 -> 4.02, (a) -> (b), IV -> V); gaps and out-of-order labels are logged
  7. namespaces Schedules / Exhibits / Annexes / Recitals / Signatures are top-level parts with their own numbering
  8. ids        stable, built from normalised labels: D-1:s4.2, D-1:s4.2.a.i, D-1:sart4, D-1:sschA.1
Every decision is recorded for the Dev Console (Document lifecycle, step 5).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from docling_core.types.doc import DoclingDocument

from .schemas import Section

# ------------------------------------------------------------------------------------------ grammar
SEP = r"[\s.:\-–—]*"
ARTICLE_RE = re.compile(rf"^(?:ARTICLE|Article)\s+([IVXLC]+|\d+)\b{SEP}(.*)$")
SECTION_RE = re.compile(r"^(?:SECTION|Section|§)\s*(\d+(?:\.\d+)*)\.?\s*(.*)$")
ATTACH_RE = re.compile(rf"^(SCHEDULE|Schedule|EXHIBIT|Exhibit|ANNEX|Annex|APPENDIX|Appendix)\s+([A-Z]|\d+)\b{SEP}(.*)$")
DECIMAL_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3})*)(\.?)\s+(\S.*)$")
PAREN_RE = re.compile(r"^\(([a-z]{1,4}|[A-Z]{1,4}|\d{1,3})\)\s*(.*)$")
RUN_IN_RE = re.compile(r"^([A-Z][^.]{0,70}?)\.\s+(\S.*)$")
TOC_LINE_RE = re.compile(r"(\.{3,}|…|\s{3,})\s*\d{1,4}\s*$")
ROMAN_RE = re.compile(r"^(?=[ivxlcdm]+$)m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")
NOISE = [re.compile(p, re.I) for p in (r"^confidential\b.{0,60}$", r"^page \d+( of \d+)?$", r"^_{5,}$", r"^\[?signature page follows\]?$")]
PART_KEYS = {"schedule": "sch", "exhibit": "exh", "annex": "annex", "appendix": "app"}


def roman_to_int(s: str) -> int:
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    s, total = s.lower(), 0
    for i, ch in enumerate(s):
        v = vals[ch]
        total += -v if i + 1 < len(s) and vals[s[i + 1]] > v else v
    return total


def norm_decimal(num: str) -> str:
    return ".".join(str(int(p)) for p in num.split("."))


@dataclass
class Label:
    style: str  # article | decimal | alpha_lower | roman_lower | alpha_upper | roman_upper | arabic_paren | part | recitals | signature
    norm: str  # art4 | 4.2 | a | iv | A | 1 | schA | recitals | signature
    ordinal: int | None
    heading: str
    body: str
    depth: int = 1  # decimal depth


# ------------------------------------------------------------------------------------------ builder state
@dataclass
class Node:
    label: Label | None
    rank: int
    sid: str
    parent: "Node | None"
    heading: str
    items: list = field(default_factory=list)  # (kind, text|md, item)
    head_item: object = None
    children: list = field(default_factory=list)
    last_child_ord: dict = field(default_factory=dict)  # style -> last ordinal among children


class Builder:
    def __init__(self, doc_id: str):
        self.doc_id = doc_id
        self.decisions: list[dict] = []
        self.root = Node(None, -10, f"{doc_id}:s0", None, "Preamble")
        self.stack: list[Node] = [self.root]
        self.paren_order: list[str] = []
        self.used_ids: Counter = Counter()
        self.nodes: list[Node] = [self.root]
        self.title: str | None = None

    # -------------------------------------------------------------- decisions
    def log(self, step: str, item, text: str, reason: str, **kw) -> None:
        self.decisions.append({"step": step, "ref": getattr(item, "self_ref", None), "label": getattr(getattr(item, "label", None), "value", None),
                               "text": (text or "")[:120], "reason": reason, **kw})

    # -------------------------------------------------------------- label parsing
    def classify_paren(self, tok: str) -> tuple[str, int]:
        """Resolve (i)/(v)/(x)/(c)... by context: continue the open sibling sequence if it fits."""
        top = self.stack[-1]
        parent = top.parent if top.label and top.label.style in ("alpha_lower", "roman_lower", "alpha_upper", "roman_upper", "arabic_paren") else top
        if tok.isdigit():
            return "arabic_paren", int(tok)
        lower = tok.islower()
        is_roman = bool(ROMAN_RE.match(tok.lower()))
        alpha_style, roman_style = ("alpha_lower", "roman_lower") if lower else ("alpha_upper", "roman_upper")
        alpha_ord = ord(tok.lower()) - 96 if len(tok) == 1 else None
        if not is_roman:
            return alpha_style, alpha_ord or 0
        if alpha_ord is None:  # ii, iv, xii ... only roman
            return roman_style, roman_to_int(tok)
        # single character that is both a letter and a roman numeral: i v x l c d m
        candidates = [n for n in (self.stack[-1], *(reversed(self.stack[:-1]))) if n.label]
        open_alpha = next((n for n in candidates if n.label.style == alpha_style), None)
        open_roman = next((n for n in candidates if n.label.style == roman_style), None)
        if open_roman and roman_to_int(tok) == (open_roman.label.ordinal or 0) + 1:
            return roman_style, roman_to_int(tok)
        if open_alpha and alpha_ord == (open_alpha.label.ordinal or 0) + 1:
            return alpha_style, alpha_ord
        last_alpha = parent.last_child_ord.get(alpha_style) if parent else None
        if last_alpha and alpha_ord == last_alpha + 1:
            return alpha_style, alpha_ord
        if tok.lower() == "i":
            return roman_style, 1
        return alpha_style, alpha_ord

    def parse_label(self, text: str, item, is_heading: bool) -> Label | None:
        t = text.strip()
        m = ATTACH_RE.match(t)
        if m and (is_heading or len(t) < 90):
            kind, ident, rest = m.group(1).lower(), m.group(2), m.group(3).strip()
            return Label("part", f"{PART_KEYS[kind]}{ident}", None, f"{m.group(1).title()} {ident}" + (f" - {rest}" if rest else ""), "")
        m = ARTICLE_RE.match(t)
        if m:
            n = m.group(1)
            num = int(n) if n.isdigit() else roman_to_int(n)
            heading, body = self.split_run_in(m.group(2).strip(), is_heading)
            return Label("article", f"art{num}", num, heading.title() if heading.isupper() else heading, body)
        m = SECTION_RE.match(t)
        if m:
            num = norm_decimal(m.group(1))
            heading, body = self.split_run_in(m.group(2).strip(), is_heading)
            return Label("decimal", num, int(num.split(".")[-1]), heading, body, depth=num.count(".") + 1)
        m = DECIMAL_RE.match(t)
        if m and (is_heading or "." in m.group(1) or m.group(2) == "."):
            num = norm_decimal(m.group(1))
            if "." not in m.group(1) and int(m.group(1)) > 60:  # "2021 was ..." / "90 days ..."
                return None
            heading, body = self.split_run_in(m.group(3).strip(), is_heading)
            if not is_heading and not body and len(heading.split()) > 10:  # a numbered sentence, not a heading
                heading, body = "", heading
            return Label("decimal", num, int(num.split(".")[-1]), heading, body, depth=num.count(".") + 1)
        m = PAREN_RE.match(t)
        if m:
            style, ordinal = self.classify_paren(m.group(1))
            body = m.group(2).strip()
            return Label(style, m.group(1), ordinal, "", body)
        upper = t.upper()
        if upper.startswith(("RECITALS", "WHEREAS")) and (is_heading or upper.startswith("WHEREAS")):
            return Label("recitals", "recitals", None, "Recitals", "" if upper.startswith("RECITALS") else t)
        if upper.startswith(("SIGNATURES", "SIGNATURE PAGE", "IN WITNESS WHEREOF")):
            return Label("signature", "signature", None, "Signature Page", "" if is_heading and not upper.startswith("IN WITNESS") else t)
        return None

    @staticmethod
    def split_run_in(rest: str, is_heading: bool) -> tuple[str, str]:
        if not rest:
            return "", ""
        m = RUN_IN_RE.match(rest)
        if m and len(m.group(1).split()) <= 8 and not is_heading:
            return m.group(1).strip(), m.group(2).strip()
        if is_heading or len(rest.split()) <= 8:
            return rest.rstrip(". "), ""
        return "", rest

    # -------------------------------------------------------------- ranking
    def rank_of(self, lab: Label) -> int:
        if lab.style in ("part", "recitals", "signature"):
            return -1
        if lab.style == "article":
            return 0
        if lab.style == "decimal":
            return 1 + lab.depth
        if lab.style not in self.paren_order:
            self.paren_order.append(lab.style)
        return 10 + self.paren_order.index(lab.style)

    def make_id(self, lab: Label, parent: Node) -> str:
        if lab.style in ("part", "recitals", "signature", "article", "unnumbered"):
            base = lab.norm
        elif lab.style == "decimal":
            ns = next((n for n in self.ancestors(parent) if n.label and n.label.style == "part"), None)
            base = f"{ns.label.norm}.{lab.norm}" if ns else lab.norm
        else:
            pid = parent.sid.split(":s", 1)[1] if parent and parent is not self.root else "0"
            base = f"{pid}.{lab.norm}"
        sid = f"{self.doc_id}:s{base}"
        self.used_ids[sid] += 1
        if self.used_ids[sid] > 1:
            self.log("duplicate", None, sid, f"label repeated - id suffixed ~{self.used_ids[sid]}")
            sid = f"{sid}~{self.used_ids[sid]}"
        return sid

    @staticmethod
    def ancestors(n: Node | None):
        while n:
            yield n
            n = n.parent

    # -------------------------------------------------------------- placing nodes
    def open_node(self, lab: Label, item, raw: str, rank: int | None = None) -> Node:
        rank = self.rank_of(lab) if rank is None else rank
        if rank == -1:  # new top-level part: reset the stack
            self.stack = [self.root]
        else:
            # recitals and signature pages hold text, not numbered sections: an article or section closes them
            if lab.style in ("article", "decimal") and any(n.label and n.label.style in ("recitals", "signature") for n in self.stack):
                self.stack = [self.root]
            while len(self.stack) > 1 and self.stack[-1].rank >= rank:
                self.stack.pop()
            # parts (schedules) keep their own numbering below them
        parent = self.stack[-1]
        # sequence validation among siblings of the same style
        last = parent.last_child_ord.get(lab.style)
        if lab.ordinal is not None:
            if last is not None and lab.ordinal != last + 1:
                self.log("sequence", item, raw, f"{lab.style} {lab.norm} follows {last} - expected {last + 1}", number=lab.norm)
            parent.last_child_ord[lab.style] = lab.ordinal
        if lab.style == "decimal" and parent.label and parent.label.style == "article" and lab.depth > 1:
            if lab.norm.split(".")[0] != str(parent.label.ordinal):
                self.log("sequence", item, raw, f"Section {lab.norm} sits in Article {parent.label.ordinal}", number=lab.norm)
        sid = self.make_id(lab, parent)
        heading = lab.heading or (f"({lab.norm}) " + " ".join(lab.body.split()[:8]) + ("…" if len(lab.body.split()) > 8 else "")
                                  if lab.style not in ("decimal", "article") else lab.norm)
        node = Node(lab, rank, sid, parent, heading, head_item=item)
        if lab.body:
            node.items.append(("text", lab.body, item))
        parent.children.append(node)
        self.stack.append(node)
        self.nodes.append(node)
        self.log("label", item, raw, f"{lab.style} '{lab.norm}' → level {len(self.stack) - 1} under {parent.sid.split(':')[-1]}",
                 number=lab.norm, sid=sid)
        return node

    def add_content(self, kind: str, payload: str, item) -> None:
        self.stack[-1].items.append((kind, payload, item))


# ------------------------------------------------------------------------------------------ cleaning
def _zone(item, doc: DoclingDocument) -> str | None:
    p = item.prov[0] if getattr(item, "prov", None) else None
    if not p or not doc.pages or p.page_no not in doc.pages:
        return None
    h = doc.pages[p.page_no].size.height
    origin = str(getattr(p.bbox.coord_origin, "value", p.bbox.coord_origin)).upper()
    top_y = p.bbox.t if "BOTTOM" in origin else h - p.bbox.t
    bot_y = p.bbox.b if "BOTTOM" in origin else h - p.bbox.b
    if top_y > 0.92 * h:
        return "top"
    if bot_y < 0.08 * h:
        return "bottom"
    return None


def _norm_rep(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text.lower())).strip()


def clean_items(doc: DoclingDocument, b: Builder) -> list:
    items = [it for it, _ in doc.iterate_items()]
    n_pages = len(doc.pages) or 0
    # running headers/footers: same normalised text in the top/bottom band on many pages
    rep_pages: dict[str, set] = defaultdict(set)
    for it in items:
        t = (getattr(it, "text", "") or "").strip()
        z = _zone(it, doc) if t else None
        if z or it.label.value in ("page_header", "page_footer"):
            rep_pages[_norm_rep(t)].add(it.prov[0].page_no if it.prov else -1)
    repeated = {k for k, pages in rep_pages.items() if n_pages and len(pages) >= max(3, 0.3 * n_pages)}
    # table of contents pages: early pages with several "... 12" lines or a TABLE OF CONTENTS heading
    toc_pages: set = set()
    by_page: dict[int, list] = defaultdict(list)
    for it in items:
        if getattr(it, "prov", None):
            by_page[it.prov[0].page_no].append(it)
    early = max(3, int(0.15 * n_pages)) if n_pages else 0
    for pg, its in by_page.items():
        if pg > early:
            continue
        texts = [(getattr(i, "text", "") or "") for i in its]
        if sum(1 for t in texts if TOC_LINE_RE.search(t)) >= 3 or any("TABLE OF CONTENTS" in t.upper() for t in texts):
            toc_pages.add(pg)
    kept = []
    docx_toc_run = 0
    for idx, it in enumerate(items):
        label = it.label.value
        t = (getattr(it, "text", "") or "").strip()
        pg = it.prov[0].page_no if getattr(it, "prov", None) else None
        if label in ("page_header", "page_footer", "picture"):
            b.log("drop", it, t, f"layout element '{label}' is not contract content")
            continue
        if pg in toc_pages:
            b.log("drop", it, t, f"table of contents (page {pg})")
            continue
        if not n_pages and TOC_LINE_RE.search(t) and idx < 0.15 * len(items):  # DOCX: no pages, pattern only
            docx_toc_run += 1
            b.log("drop", it, t, "table of contents line")
            continue
        if t and _norm_rep(t) in repeated and (_zone(it, doc) or label in ("page_header", "page_footer")):
            b.log("drop", it, t, "running header/footer (repeats on many pages)")
            continue
        if label != "table" and t and any(p.search(t) for p in NOISE):
            b.log("drop", it, t, "boilerplate (confidentiality line / page number / signature rule)")
            continue
        kept.append(it)
    b.toc_pages = sorted(toc_pages)  # type: ignore[attr-defined]
    b.repeated = sorted(repeated)  # type: ignore[attr-defined]
    return kept


# ------------------------------------------------------------------------------------------ main entry
def _looks_like_heading(item, text: str) -> bool:
    if item.label.value != "list_item":
        return False
    words = text.split()
    return 0 < len(words) <= 6 and not text.rstrip().endswith((".", ";", ":")) and text[:1].isupper()


def _prov(item) -> tuple[list[int], list[dict]]:
    pages, boxes = [], []
    for p in getattr(item, "prov", []) or []:
        pages.append(p.page_no)
        bb = p.bbox
        boxes.append({"page": p.page_no, "l": bb.l, "t": bb.t, "r": bb.r, "b": bb.b,
                      "origin": getattr(bb.coord_origin, "value", str(bb.coord_origin))})
    return pages, boxes


def build_sections(doc_id: str, doc: DoclingDocument) -> tuple[list[Section], dict]:
    b = Builder(doc_id)
    items = clean_items(doc, b)
    pending_unnumbered: list = []  # unnumbered headings to repair from neighbours
    for item in items:
        label = item.label.value
        if label == "table":
            b.add_content("table", item.export_to_markdown(doc=doc), item)
            continue
        text = (getattr(item, "text", "") or "").strip()
        if not text:
            continue
        is_heading = label in ("section_header", "title") or _looks_like_heading(item, text)
        raw = text
        # Docling sometimes moves the number into the list marker: "Payout" with marker "2.1"
        marker = (getattr(item, "marker", "") or "").strip()
        if label == "list_item" and marker and not PAREN_RE.match(text) and not DECIMAL_RE.match(text):
            mk = marker.rstrip(".")
            if re.fullmatch(r"\d+(\.\d+)*", mk) or re.fullmatch(r"\([a-zA-Z0-9]+\)", marker):
                text = f"{mk if mk[0].isdigit() else marker} {text}"
                b.log("heading", item, raw, f"number '{marker}' recovered from Docling list marker")
        lab = b.parse_label(text, item, is_heading)
        if lab is None and label == "title" and b.title is None:
            b.title = text
            b.root.heading = text
            b.root.head_item = item
            continue
        if lab is None and is_heading:
            if label == "list_item":
                b.log("heading", item, raw, "Docling mislabelled a heading as a list item - promoted to heading")
            # unnumbered heading: keep for repair; until then it opens a child of the current node
            if len(b.nodes) == 1 and not any(n.label for n in b.nodes):  # before any numbered structure: document title
                if b.title is None:
                    b.title = text
                    b.root.heading = text
                    b.root.head_item = item
                    continue
            pending_unnumbered.append((item, text, len(b.nodes)))
            # rank like a sibling of the current numbered heading; repair fixes the number and parent later
            top = b.stack[-1]
            rank = top.rank if top.label and top.label.style in ("decimal", "unnumbered") else max(top.rank + 1, 1)
            node = b.open_node(Label("unnumbered", f"x{len(b.nodes)}", None, text, ""), item, raw, rank=rank)
            continue
        if lab is None:
            b.add_content("text", text, item)
            continue
        if lab.style in ("recitals", "signature") and any(n.label and n.label.style == lab.style for n in b.stack):
            b.add_content("text", text, item)  # more WHEREAS lines / witness text inside the same part
            continue
        b.open_node(lab, item, raw)

    # ---- repair unnumbered headings from their numbered neighbours (Docling lost the number)
    numbered = [n for n in b.nodes if n.label and n.label.style == "decimal"]
    seen = {n.label.norm for n in numbered}
    for node in [n for n in b.nodes if n.label and n.label.style == "unnumbered"]:
        idx = b.nodes.index(node)
        nxt = next((n.label.norm for n in b.nodes[idx + 1:] if n.label and n.label.style == "decimal"), None)
        prev = next((n.label.norm for n in reversed(b.nodes[:idx]) if n.label and n.label.style == "decimal"), None)
        guess = None
        if nxt and "." in nxt:
            top, sub = nxt.split(".", 1)
            if top not in seen:
                guess = top
            elif sub.isdigit() and int(sub) > 1 and prev == top:
                guess = f"{top}.{int(sub) - 1}"
        elif nxt and nxt.isdigit() and prev and int(nxt) - int(prev.split(".")[0]) == 2:
            guess = str(int(nxt) - 1)
        elif nxt is None and prev and prev.split(".")[0].isdigit():
            guess = str(int(prev.split(".")[0]) + 1)
        if guess and guess not in seen:
            seen.add(guess)
            old = node.sid
            node.label.norm, node.label.style = guess, "decimal"  # type: ignore[union-attr]
            node.sid = f"{doc_id}:s{guess}"
            b.log("repair", node.head_item, node.heading, f"unnumbered heading between {prev or '-'} and {nxt or '-'} - assigned {guess}",
                  number=guess, sid=node.sid, was=old)
    # re-parent repaired decimal nodes by number (x.y belongs under x; a top-level x never sits under another decimal)
    by_norm = {n.label.norm: n for n in b.nodes if n.label and n.label.style == "decimal"}
    for n in b.nodes:
        if n.label and n.label.style == "decimal" and "." not in n.label.norm:
            while n.parent and n.parent.label and n.parent.label.style in ("decimal", "unnumbered"):
                old = n.parent
                old.children.remove(n)
                n.parent = old.parent
                n.parent.children.append(n)
    for n in b.nodes:
        if n.label and n.label.style == "decimal" and "." in n.label.norm:
            want = by_norm.get(n.label.norm.rsplit(".", 1)[0])
            if want and n.parent is not want and n.parent and n.parent.label and n.parent.label.style != "article":
                n.parent.children.remove(n)
                want.children.append(n)
                n.parent = want
    # child ids of repaired parents follow the new number
    for n in b.nodes:
        if n.label and n.label.style not in ("decimal", "article", "part", "recitals", "signature", "unnumbered") and n.parent:
            pid = n.parent.sid.split(":s", 1)[1] if n.parent is not b.root else "0"
            n.sid = f"{doc_id}:s{pid}.{n.label.norm}"

    # ---- emit Section rows in reading order
    sections: list[Section] = []
    order = {id(n): i for i, n in enumerate(b.nodes)}

    def depth(n: Node) -> int:
        d = 0
        while n.parent:
            d, n = d + 1, n.parent
        return d

    def path(n: Node) -> list[str]:
        out = []
        while n and n is not b.root:
            out.append(n.heading)
            n = n.parent
        return list(reversed(out)) or ["Preamble"]
    for n in sorted(b.nodes, key=lambda n: order[id(n)]):
        sec = Section(section_id=n.sid, doc_id=doc_id,
                      number=None if n is b.root else n.label.norm if n.label and not n.label.norm.startswith("x") else None,
                      heading=b.title or "Preamble" if n is b.root else n.heading, path=path(n), level=depth(n),
                      parent_id=n.parent.sid if n.parent and n.parent is not b.root else None)
        if n.head_item is not None:
            pg, bx = _prov(n.head_item)
            sec.pages += pg
            sec.bboxes += bx
            sec.item_refs.append(n.head_item.self_ref)
        texts = []
        for kind, payload, item in n.items:
            if kind == "table":
                sec.tables_md.append(payload)
            else:
                texts.append(payload)
            if item is not n.head_item:
                pg, bx = _prov(item)
                sec.pages += pg
                sec.bboxes += bx
                sec.item_refs.append(item.self_ref)
        sec.text = "\n".join(texts)
        sec.pages = sorted(set(sec.pages))
        sections.append(sec)
    styles = Counter(n.label.style for n in b.nodes if n.label)
    stats = {"sections": len(sections),
             "headings_repaired": sum(1 for d in b.decisions if d["step"] == "repair" or "promoted" in d["reason"] or "recovered" in d["reason"]),
             "unnumbered_after_repair": sum(1 for n in b.nodes if n.label and n.label.style == "unnumbered"),
             "label_styles": dict(styles), "sequence_issues": sum(1 for d in b.decisions if d["step"] == "sequence"),
             "toc_pages": getattr(b, "toc_pages", []), "running_headers": getattr(b, "repeated", [])[:10],
             "dropped_items": sum(1 for d in b.decisions if d["step"] == "drop"),
             "decisions": b.decisions}
    return sections, stats
