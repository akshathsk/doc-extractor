"""Entitlements (mock). Cross-trader tools filter clause text by what the caller may see."""
from __future__ import annotations

from ..db import q, tx

SUPERUSERS = {"system", "admin"}
DEFAULT_GRANTS = {"analyst_a": [f"T-{n}" for n in range(1001, 1006)], "analyst_b": [f"T-{n}" for n in range(1006, 1011)]}


class AccessDenied(Exception):
    pass


def seed_entitlements() -> None:
    with tx() as c:
        for caller, traders in DEFAULT_GRANTS.items():
            for t in traders:
                c.execute("INSERT OR IGNORE INTO entitlements(caller, trader_id) VALUES (?,?)", (caller, t))


def allowed_traders(caller: str) -> set[str] | None:
    """None means unrestricted."""
    if caller in SUPERUSERS:
        return None
    return {r["trader_id"] for r in q("SELECT trader_id FROM entitlements WHERE caller=?", (caller,))}


def can_see(caller: str, trader_id: str) -> bool:
    allowed = allowed_traders(caller)
    return allowed is None or trader_id in allowed


def require(caller: str, trader_id: str) -> None:
    if not can_see(caller, trader_id):
        raise AccessDenied(f"caller '{caller}' is not entitled to trader {trader_id}")
