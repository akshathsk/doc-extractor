"""Evidence ledger: researchers record every occurrence of a value; nothing here decides what is current."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from ..db import j, q, q1, tx
from ..observability import current_run_id
from ..schemas import Citation, Finding, is_period_limit, known_leaves, taxonomy
from .verify import locate_quote, verify_quote


def normalize_value(value, unit: str | None):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    s = str(value).strip()
    low = s.lower()
    if low in ("true", "yes", "applies", "y"):
        return True
    if low in ("false", "no", "does not apply", "n"):
        return False
    num = re.fullmatch(r"(?:us)?\$?\s*([\d,]+(?:\.\d+)?)\s*(%|percent|months?|years?|days?)?", low.replace("us$", "$"))
    if num:
        n = float(num.group(1).replace(",", ""))
        return int(n) if n.is_integer() else n
    return s


def _check_path(path: str) -> str | None:
    parts = path.split(".")
    if len(parts) != 3:
        return "path must be Term.SubTerm.leaf_key"
    t, s, k = parts
    tx_ = taxonomy()
    if t not in tx_:
        return f"unknown term {t}; allowed: {list(tx_)}"
    if s not in tx_[t]["sub_terms"]:
        return f"unknown sub-term {t}.{s}; allowed: {list(tx_[t]['sub_terms'])}"
    kl = known_leaves().get(k)
    if kl and kl["path"] != path:
        return f"leaf {k} belongs under {kl['path']}"
    if not kl:  # invented key: reject near-duplicates of a known leaf
        import difflib
        close = difflib.get_close_matches(k, list(known_leaves()), n=1, cutoff=0.75)
        if close:
            return f"'{k}' looks like the known leaf '{close[0]}' - record it as {known_leaves()[close[0]]['path']}"
        if k.endswith(("_definition", "_provisions")):
            return "do not record definitions or provision headers as leaves; record the economic/contractual values only"
    if not re.fullmatch(r"[a-z][a-z0-9_]*", k):
        return "leaf_key must be snake_case"
    return None


def record_finding(trader_id: str, path: str, value, quote: str, doc_id: str, section_id: str, unit: str | None = None,
                   assertion: str = "set", scope_note: str | None = None, effective_date: str | None = None,
                   page: int | None = None, agent: str | None = None) -> dict:
    err = _check_path(path)
    if err:
        return {"recorded": False, "error": err}
    doc = q1("SELECT doc_id, trader_id, doc_date FROM documents WHERE doc_id=?", (doc_id,))
    if not doc or doc["trader_id"] != trader_id:
        return {"recorded": False, "error": f"{doc_id} is not a document of {trader_id}"}
    if assertion == "removed" and re.search(r"replaced with|substituted with|amended to read", quote, re.I):
        return {"recorded": False, "error": "This text REPLACES the provision; it does not remove it. Record the new value(s) "
                "from the replacement wording with assertion='set' instead."}
    warnings = []
    if scope_note and not is_period_limit(scope_note):
        warnings.append(f"scope_note dropped: '{scope_note}' is not a period limit (e.g. 'Performance Year 2025 only'). "
                        "Only use scope_note when the value applies to a specific period/subset and is temporary.")
        scope_note = None
    value = None if assertion == "removed" else normalize_value(value, unit)
    unit = unit or (known_leaves().get(path.split(".")[-1]) or {}).get("unit")
    if page is None:
        sec = q1("SELECT pages_json FROM sections WHERE section_id=?", (section_id,))
        page = (json.loads(sec["pages_json"]) or [None])[0] if sec else None
    v = verify_quote(doc_id, section_id, quote, value)
    loc = locate_quote(doc_id, section_id, quote)
    f = Finding(trader_id=trader_id, path=path, value=value, unit=unit, assertion=assertion, scope_note=scope_note,
                source=Citation(doc_id=doc_id, section_id=section_id, doc_date=doc["doc_date"], effective_date=effective_date,
                                page=loc.get("page") or page, quote=quote, docling_ref=loc.get("docling_ref"), bbox=loc.get("bbox")),
                quote_verified=v["verified"], run_id=current_run_id(), agent=agent)
    with tx() as c:
        cur = c.execute("INSERT INTO findings(run_id, trader_id, path, value_json, unit, assertion, scope_note, source_json,"
                        " quote_verified, agent, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (f.run_id, trader_id, path, j(value), unit, assertion, scope_note, f.source.model_dump_json(),
                         int(bool(v["verified"])), agent, datetime.now(timezone.utc).isoformat()))
    out = {"recorded": True, "finding_id": cur.lastrowid, "quote_verified": v["verified"], "verify_reason": v["reason"],
           "value": value}
    if warnings:
        out["warnings"] = warnings
    return out


def _row_to_finding(r: dict) -> Finding:
    return Finding(finding_id=r["finding_id"], trader_id=r["trader_id"], path=r["path"], value=json.loads(r["value_json"]),
                   unit=r["unit"], assertion=r["assertion"], scope_note=r["scope_note"],
                   source=Citation.model_validate_json(r["source_json"]), quote_verified=bool(r["quote_verified"]),
                   run_id=r["run_id"], agent=r["agent"])


def get_findings(trader_id: str, run_id: str | None = None, path: str | None = None) -> list[Finding]:
    run_id = run_id or current_run_id()
    sql = "SELECT * FROM findings WHERE trader_id=? AND run_id=?"
    params: list = [trader_id, run_id]
    if path:
        sql += " AND (path=? OR path LIKE ?)"
        params += [path, path + ".%"]
    return [_row_to_finding(r) for r in q(sql + " ORDER BY finding_id", tuple(params))]
