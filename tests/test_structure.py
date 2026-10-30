"""Section builder on real-contract numbering (structure.py)."""
from docling_core.types.doc import DocItemLabel, DoclingDocument

from compdoc.structure import build_sections, roman_to_int


def make(lines: list[tuple[str, str]]) -> DoclingDocument:
    """lines: (kind, text) with kind in heading | text."""
    d = DoclingDocument(name="t")
    for kind, text in lines:
        if kind == "heading":
            d.add_heading(text=text, level=1)
        else:
            d.add_text(label=DocItemLabel.TEXT, text=text)
    return d


def tree(lines):
    secs, stats = build_sections("D", make(lines))
    return {s.section_id: s for s in secs}, stats


def test_roman_numerals():
    assert roman_to_int("iv") == 4 and roman_to_int("XIV") == 14 and roman_to_int("ix") == 9


def test_article_section_run_in_and_clauses():
    t, _ = tree([
        ("heading", "ARTICLE IV - COMPENSATION"),
        ("text", "Section 4.02 Payout. The Trader shall be entitled to seventeen percent (17%) of Net Trading P&L."),
        ("text", "(a) the amount is determined by the Firm;"),
        ("text", "(i) subject to continued employment;"),
        ("text", "(ii) subject to no notice having been given;"),
        ("text", "(b) the determination is final."),
        ("text", "Section 4.03 Hurdle. No payout accrues below US$2,000,000."),
    ])
    assert t["D:sart4"].heading == "Compensation"
    s = t["D:s4.2"]  # zero padding removed
    assert s.heading == "Payout" and "seventeen percent" in s.text and s.parent_id == "D:sart4"
    assert t["D:s4.2.a"].parent_id == "D:s4.2"
    assert t["D:s4.2.a.i"].parent_id == "D:s4.2.a" and t["D:s4.2.a.ii"].parent_id == "D:s4.2.a"
    assert t["D:s4.2.b"].parent_id == "D:s4.2"  # (b) pops back past (i)/(ii)
    assert t["D:s4.3"].parent_id == "D:sart4"


def test_letter_i_after_h_is_a_letter_and_v_after_iv_is_roman():
    letters = [("text", f"({c}) clause {c};") for c in "abcdefghi"]
    t, _ = tree([("text", "Section 2.01 Terms. The following apply:")] + letters)
    assert "D:s2.1.i" in t and t["D:s2.1.i"].parent_id == "D:s2.1"  # a 9th letter, sibling of (h)
    romans = [("text", f"({r}) item;") for r in ["i", "ii", "iii", "iv", "v"]]
    t, _ = tree([("text", "Section 3.01 Items. As follows:"), ("text", "(a) first group:")] + romans)
    assert t["D:s3.1.a.v"].parent_id == "D:s3.1.a"  # (v) continues the roman list under (a)


def test_schedule_namespace_and_recitals():
    t, _ = tree([
        ("heading", "RECITALS"),
        ("text", "WHEREAS, the Firm wishes to employ the Trader;"),
        ("text", "WHEREAS, the Trader accepts;"),
        ("heading", "ARTICLE I - DEFINITIONS"),
        ("text", "Section 1.01 Terms. Words have their defined meanings."),
        ("heading", "Schedule A - Payout Grid"),
        ("text", "1. Grid. The grid below applies."),
        ("heading", "Exhibit B - Form of Release"),
    ])
    assert t["D:srecitals"].text.count("WHEREAS") == 2  # one recitals part, not two
    assert t["D:sart1"].parent_id is None  # articles are not nested under recitals
    assert t["D:sschA.1"].parent_id == "D:sschA"  # numbering inside a schedule is its own namespace
    assert "D:sexhB" in t


def test_sequence_gap_is_logged():
    _, stats = tree([("text", "Section 5.01 A. x."), ("text", "Section 5.03 C. y.")])
    assert stats["sequence_issues"] == 1


def test_plain_decimal_documents_unchanged():
    t, _ = tree([("heading", "Employment Agreement"), ("heading", "2. Compensation"), ("heading", "2.1 Base Salary"),
                 ("text", "The Trader shall receive US$200,000."), ("heading", "2.2 Payout"), ("text", "Fifteen percent (15%).")])
    assert t["D:s2.1"].parent_id == "D:s2" and t["D:s2.2"].heading == "Payout" and t["D:s0"].heading == "Employment Agreement"


def test_toc_lines_dropped_in_docx():
    lines = [("heading", "Agreement"), ("text", "ARTICLE I Definitions ........ 3"), ("text", "ARTICLE II Term ........ 5"),
             ("text", "ARTICLE III Duties ........ 7")] + [("text", f"Section 1.{i:02d} T{i}. Body text {i}.") for i in range(1, 30)]
    t, stats = tree(lines)
    assert not any("....." in s.text for s in t.values())
    assert sum(1 for d in stats["decisions"] if "table of contents" in d["reason"]) == 3
