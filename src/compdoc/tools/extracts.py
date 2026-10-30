"""Published, versioned trader extracts."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..db import q, q1, tx
from ..schemas import TraderExtract
from .access import require


def publish_extract(extract: TraderExtract) -> TraderExtract:
    with tx() as c:
        v = c.execute("SELECT coalesce(max(version),0)+1 FROM extracts WHERE trader_id=?", (extract.trader_id,)).fetchone()[0]
        extract.version = v
        c.execute("INSERT INTO extracts(trader_id, version, run_id, as_of, extract_json, created_at) VALUES (?,?,?,?,?,?)",
                  (extract.trader_id, v, extract.run_id, extract.as_of, extract.model_dump_json(),
                   datetime.now(timezone.utc).isoformat()))
    return extract


def latest_extract(trader_id: str, version: int | None = None) -> TraderExtract | None:
    sql = "SELECT extract_json FROM extracts WHERE trader_id=?" + (" AND version=?" if version else "") + " ORDER BY version DESC LIMIT 1"
    r = q1(sql, (trader_id, version) if version else (trader_id,))
    return TraderExtract.model_validate_json(r["extract_json"]) if r else None


def extract_versions(trader_id: str) -> list[dict]:
    return q("SELECT version, run_id, as_of, created_at FROM extracts WHERE trader_id=? ORDER BY version", (trader_id,))


def get_trader_extract(trader_id: str, caller: str = "system") -> dict:
    require(caller, trader_id)
    e = latest_extract(trader_id)
    if not e:
        return {"error": f"no published extract for {trader_id}; run extraction first"}
    return e.model_dump(exclude={"documents"})


def get_term(trader_id: str, path: str, caller: str = "system") -> dict:
    """path may be Term, Term.SubTerm or Term.SubTerm.leaf_key."""
    require(caller, trader_id)
    e = latest_extract(trader_id)
    if not e:
        return {"error": f"no published extract for {trader_id}"}
    hits = {p: lf.model_dump() for p, lf in e.flat().items() if p == path or p.startswith(path + ".")}
    return {"trader_id": trader_id, "version": e.version, "as_of": e.as_of, "leaves": hits} if hits else \
        {"error": f"{path} not found in extract v{e.version}", "available_paths": sorted(e.flat())}


def compare_versions(trader_id: str, from_version: int, to_version: int, caller: str = "system") -> dict:
    require(caller, trader_id)
    a, b = latest_extract(trader_id, from_version), latest_extract(trader_id, to_version)
    if not a or not b:
        return {"error": "version not found"}
    fa, fb = a.flat(), b.flat()
    changes = []
    for p in sorted(set(fa) | set(fb)):
        va, vb = (fa[p].value if p in fa else None), (fb[p].value if p in fb else None)
        sa, sb = (fa[p].status if p in fa else None), (fb[p].status if p in fb else None)
        if va != vb or sa != sb:
            changes.append({"path": p, "from": va, "to": vb, "from_status": sa, "to_status": sb})
    return {"trader_id": trader_id, "from": from_version, "to": to_version, "changes": changes}


def diff_json(trader_id: str) -> str:
    vs = extract_versions(trader_id)
    if len(vs) < 2:
        return json.dumps({"changes": []})
    return json.dumps(compare_versions(trader_id, vs[-2]["version"], vs[-1]["version"]))
