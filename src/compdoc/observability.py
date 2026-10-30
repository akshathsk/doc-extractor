"""Tracing: runs -> spans tree (node / agent / llm / tool), stored in SQLite for the Dev Console.

Usage:
    with start_run("extract", trader_id="T-1003") as run_id: ...
    @traced("tool")            def read_section(...): ...
    with span("agent", "researcher:Compensation", input={...}) as s: s.output = ...; s.add_usage(...)

The parent span is tracked with contextvars, so nested calls build a tree automatically, including across
LangGraph worker threads (we copy the context when fanning out).
"""
from __future__ import annotations

import contextvars
import functools
import itertools
import logging
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

import structlog

from .config import PRICING, settings
from .db import j, q1, tx

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(
    getattr(logging, __import__("os").environ.get("COMPDOC_LOG_LEVEL", "WARNING"))))
log = structlog.get_logger()

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
_parent: contextvars.ContextVar[str | None] = contextvars.ContextVar("parent_span", default=None)
_seq = itertools.count()

SECRET_KEYS = ("api_key", "authorization", "password")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_run_id() -> str | None:
    return _run_id.get()


def _safe(v: Any, limit: int = 60_000) -> str:
    try:
        s = json.dumps(v, default=lambda o: o.model_dump() if hasattr(o, "model_dump") else str(o))
    except Exception:
        s = json.dumps(str(v))
    for k in SECRET_KEYS:
        if k in s.lower():
            s = s.replace(k, "[redacted-key]")
    if settings.redact_spans:
        return json.dumps({"redacted": True, "chars": len(s)})
    return s if len(s) <= limit else s[:limit] + '..."[truncated]"'


def cost_of(model: str | None, tin: int, tout: int) -> float:
    pin, pout = PRICING.get(model or "", (0.0, 0.0))
    return round(tin / 1e6 * pin + tout / 1e6 * pout, 6)


@dataclass
class Span:
    kind: str
    name: str
    input: Any = None
    output: Any = None
    model: str | None = None
    prompt_version: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    def add_usage(self, model: str, tin: int, tout: int) -> None:
        self.model = model
        self.tokens_in += tin
        self.tokens_out += tout


@contextmanager
def span(kind: str, name: str, input: Any = None, **kw: Any) -> Iterator[Span]:
    s = Span(kind=kind, name=name, input=input, **kw)
    run_id = _run_id.get()
    parent = _parent.get()
    token = _parent.set(s.span_id)
    t0 = time.perf_counter()
    started = now()
    status, err = "ok", None
    try:
        yield s
    except Exception as e:  # noqa: BLE001
        status, err = "error", f"{type(e).__name__}: {e}"
        raise
    finally:
        _parent.reset(token)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        cost = cost_of(s.model, s.tokens_in, s.tokens_out)
        if run_id:
            with tx() as c:
                c.execute(
                    "INSERT INTO spans(span_id, run_id, parent_id, kind, name, input_json, output_json, model, prompt_version,"
                    " tokens_in, tokens_out, cost, started_at, ended_at, latency_ms, status, error, seq)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (s.span_id, run_id, parent, kind, name, _safe(s.input), _safe(s.output), s.model, s.prompt_version,
                     s.tokens_in, s.tokens_out, cost, started, now(), ms, status, err, next(_seq)))
                if s.tokens_in or s.tokens_out:
                    c.execute("UPDATE runs SET tokens_in=tokens_in+?, tokens_out=tokens_out+?, cost=cost+? WHERE run_id=?",
                              (s.tokens_in, s.tokens_out, cost, run_id))
        log.debug("span", kind=kind, name=name, ms=ms, status=status)


def traced(kind: str, name: str | None = None) -> Callable:
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            inp = kwargs if not args else {"args": [a for a in args if isinstance(a, (str, int, float, bool, dict, list))], **kwargs}
            with span(kind, name or fn.__name__, input=inp) as s:
                out = fn(*args, **kwargs)
                s.output = out
                return out
        return wrapper
    return deco


@contextmanager
def start_run(run_type: str, trader_id: str | None = None, caller: str = "system", input: Any = None,
              run_id: str | None = None) -> Iterator[str]:
    """Open (or resume) a run. Resuming an existing run_id appends spans to it."""
    run_id = run_id or f"{run_type}-{uuid.uuid4().hex[:10]}"
    existing = q1("SELECT run_id FROM runs WHERE run_id=?", (run_id,))
    with tx() as c:
        if existing:
            c.execute("UPDATE runs SET status='running', ended_at=NULL, error=NULL WHERE run_id=?", (run_id,))
        else:
            c.execute("INSERT INTO runs(run_id, type, trader_id, caller, status, input_json, started_at) VALUES (?,?,?,?,?,?,?)",
                      (run_id, run_type, trader_id, caller, "running", _safe(input), now()))
    t_run = _run_id.set(run_id)
    t_par = _parent.set(None)
    t0 = time.perf_counter()
    status, err = "ok", None
    try:
        yield run_id
    except Exception as e:  # noqa: BLE001
        status, err = "error", f"{type(e).__name__}: {e}"
        raise
    finally:
        _run_id.reset(t_run)
        _parent.reset(t_par)
        with tx() as c:
            c.execute("UPDATE runs SET status=CASE WHEN status='interrupted' AND ?='ok' THEN 'interrupted' ELSE ? END,"
                      " ended_at=?, latency_ms=COALESCE(latency_ms,0)+?, error=? WHERE run_id=?",
                      (status, status, now(), round((time.perf_counter() - t0) * 1000, 1), err, run_id))


def record_span(kind: str, name: str, input: Any = None, output: Any = None, latency_ms: float = 0.0, status: str = "ok",
                error: str | None = None) -> None:
    """Record a span for work done elsewhere (e.g. a shard converted in a worker process)."""
    run_id = _run_id.get()
    if not run_id:
        return
    with tx() as c:
        c.execute("INSERT INTO spans(span_id, run_id, parent_id, kind, name, input_json, output_json, tokens_in, tokens_out, cost,"
                  " started_at, ended_at, latency_ms, status, error, seq) VALUES (?,?,?,?,?,?,?,0,0,0,?,?,?,?,?,?)",
                  (uuid.uuid4().hex[:16], run_id, _parent.get(), kind, name, _safe(input), _safe(output), now(), now(),
                   latency_ms, status, error, next(_seq)))


@contextmanager
def in_run(run_id: str) -> Iterator[None]:
    """Re-attach to a run inside worker threads (LangGraph nodes) so spans land under the right run."""
    t_run = _run_id.set(run_id)
    t_par = _parent.set(None)
    try:
        yield
    finally:
        _run_id.reset(t_run)
        _parent.reset(t_par)


def set_run_status(run_id: str, status: str, output: Any = None) -> None:
    with tx() as c:
        c.execute("UPDATE runs SET status=?, output_json=COALESCE(?, output_json) WHERE run_id=?",
                  (status, _safe(output) if output is not None else None, run_id))


def copy_context() -> contextvars.Context:
    return contextvars.copy_context()


def j_dump(v: Any) -> str:
    return j(v)
