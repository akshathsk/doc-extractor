"""Provider-agnostic LLM layer: structured outputs + a traced tool-calling agent loop.

Providers:
  openai     Chat Completions (function calling, `parse` for structured output)
  anthropic  Messages API (tool use, `parse` for structured output)
  offline    no LLM: each call site supplies a deterministic fallback/policy that uses the same tools

Every LLM call is an `llm` span; every tool execution is a `tool` span, so the Dev Console shows the
exact prompt, tool calls, tool results and token usage for each agent turn.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .config import settings
from .observability import span

T = TypeVar("T", bound=BaseModel)


# ------------------------------------------------------------------------------------------- tools
@dataclass
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: Callable[..., Any]

    def schema(self) -> dict:
        s = self.input_model.model_json_schema()
        s.pop("title", None)
        for p in s.get("properties", {}).values():
            p.pop("title", None)
        return s

    def openai_spec(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.schema()}}

    def anthropic_spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema()}

    def run(self, args: dict) -> tuple[str, bool]:
        """Validate + execute inside a tool span. Returns (json_text, is_error)."""
        with span("tool", self.name, input=args) as s:
            try:
                parsed = self.input_model.model_validate(args)
            except ValidationError as e:
                s.output = {"error": str(e)}
                return json.dumps({"error": f"invalid arguments: {e.errors(include_url=False)}"}), True
            try:
                out = self.fn(**parsed.model_dump())
            except Exception as e:  # noqa: BLE001  - surface tool failures to the model, don't crash the agent
                s.output = {"error": f"{type(e).__name__}: {e}"}
                return json.dumps({"error": f"{type(e).__name__}: {e}"}), True
            s.output = out
            return json.dumps(out, default=lambda o: o.model_dump() if hasattr(o, "model_dump") else str(o)), False


class ToolBox:
    """Lets offline policies call tools exactly like an LLM would (same validation + tracing)."""

    def __init__(self, tools: list[Tool]):
        self.tools = {t.name: t for t in tools}
        self.calls: list[dict] = []

    def call(self, name: str, **args: Any) -> Any:
        text, err = self.tools[name].run(args)
        result = json.loads(text)
        self.calls.append({"tool": name, "args": args, "error": err, "result": result})
        return result


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


@dataclass
class AgentResult:
    final_text: str
    steps: int
    tool_calls: list[dict] = field(default_factory=list)
    messages: list[Any] = field(default_factory=list)


# ------------------------------------------------------------------------------------------- clients
_clients: dict[str, Any] = {}


def _client(provider: str) -> Any:
    if provider not in _clients:
        if provider == "openai":
            from openai import OpenAI
            _clients[provider] = OpenAI(max_retries=3, timeout=180)
        elif provider == "anthropic":
            import anthropic
            _clients[provider] = anthropic.Anthropic(max_retries=3)
        else:
            raise ValueError(provider)
    return _clients[provider]


def provider() -> str:
    return settings.provider


def _openai_extra(model: str, effort: str | None) -> dict:
    return {"reasoning_effort": effort} if effort and model.startswith(("gpt-5", "o")) else {}


# ------------------------------------------------------------------------------------------- structured
def structured(*, name: str, system: str, user: str, schema: type[T], offline: Callable[[], T],
               model: str | None = None, prompt_version: str = "v1", effort: str | None = "low") -> T:
    """One LLM call that returns a validated `schema` instance."""
    model = model or settings.fast_model
    p = provider()
    with span("llm", name, input={"system": system, "user": user, "schema": schema.__name__},
              prompt_version=prompt_version) as s:
        if p == "offline":
            s.model = "offline"
            out = offline()
        elif p == "openai":
            r = _client(p).chat.completions.parse(
                model=model, response_format=schema,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **_openai_extra(model, effort))
            s.add_usage(model, r.usage.prompt_tokens, r.usage.completion_tokens)
            msg = r.choices[0].message
            if msg.parsed is None:
                raise RuntimeError(f"model returned no parsed output (refusal={msg.refusal!r})")
            out = msg.parsed
        else:
            r = _client(p).messages.parse(model=model, max_tokens=16000, system=system, output_format=schema,
                                          messages=[{"role": "user", "content": user}])
            s.add_usage(model, r.usage.input_tokens, r.usage.output_tokens)
            out = r.parsed_output
        s.output = out.model_dump()
        return out


def complete(*, name: str, system: str, user: str, offline: Callable[[], str], model: str | None = None,
             prompt_version: str = "v1", effort: str | None = "low") -> str:
    """Plain text completion (summaries, router fallbacks)."""
    model = model or settings.agent_model
    p = provider()
    with span("llm", name, input={"system": system, "user": user}, prompt_version=prompt_version) as s:
        if p == "offline":
            s.model = "offline"
            out = offline()
        elif p == "openai":
            r = _client(p).chat.completions.create(
                model=model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **_openai_extra(model, effort))
            s.add_usage(model, r.usage.prompt_tokens, r.usage.completion_tokens)
            out = r.choices[0].message.content or ""
        else:
            r = _client(p).messages.create(model=model, max_tokens=16000, system=system,
                                           messages=[{"role": "user", "content": user}])
            s.add_usage(model, r.usage.input_tokens, r.usage.output_tokens)
            out = "".join(b.text for b in r.content if b.type == "text")
        s.output = out
        return out


# ------------------------------------------------------------------------------------------- agent loop
def run_agent(*, name: str, system: str, user: str, tools: list[Tool],
              offline_policy: Callable[[ToolBox], str], model: str | None = None,
              max_steps: int | None = None, prompt_version: str = "v1", effort: str | None = "low",
              history: list[dict] | None = None) -> AgentResult:
    """Run a tool-calling agent until it stops calling tools (or hits max_steps).

    `history` (chat only) is prior [{role, content}] turns, inserted between system and the new user message.
    """
    model = model or settings.agent_model
    max_steps = max_steps or settings.max_agent_steps
    p = provider()
    with span("agent", name, input={"user": user, "tools": [t.name for t in tools]}, prompt_version=prompt_version) as agent_span:
        if p == "offline":
            box = ToolBox(tools)
            agent_span.model = None
            text = offline_policy(box)
            agent_span.output = {"final": text, "tool_calls": len(box.calls)}
            return AgentResult(text, steps=len(box.calls), tool_calls=box.calls)
        if p == "openai":
            res = _openai_loop(name, system, user, tools, model, max_steps, prompt_version, effort, history or [])
        else:
            res = _anthropic_loop(name, system, user, tools, model, max_steps, prompt_version, history or [])
        agent_span.output = {"final": res.final_text, "steps": res.steps, "tool_calls": len(res.tool_calls)}
        return res


def _openai_loop(name: str, system: str, user: str, tools: list[Tool], model: str, max_steps: int,
                 prompt_version: str, effort: str | None, history: list[dict]) -> AgentResult:
    """Responses API loop (function tools + reasoning). store=False: OpenAI keeps no copy of contract text;
    reasoning is carried between turns as encrypted content."""
    client = _client("openai")
    by_name = {t.name: t for t in tools}
    specs = [{"type": "function", "name": t.name, "description": t.description, "parameters": t.schema(), "strict": False}
             for t in tools]
    items: list[Any] = [*history, {"role": "user", "content": user}]
    calls: list[dict] = []
    extra: dict[str, Any] = {"store": False, "include": ["reasoning.encrypted_content"]}
    if effort and model.startswith(("gpt-5", "o")):
        extra["reasoning"] = {"effort": effort}
    for step in range(1, max_steps + 1):
        with span("llm", f"{name}:turn{step}", input={"instructions": system if step == 1 else "(same)",
                                                      "new_items": items[-4:], "n_items": len(items)},
                  prompt_version=prompt_version) as s:
            r = client.responses.create(model=model, instructions=system, input=items, tools=specs or None, **extra)
            s.add_usage(model, r.usage.input_tokens, r.usage.output_tokens)
            fcalls = [o for o in r.output if o.type == "function_call"]
            s.output = {"text": r.output_text, "function_calls": [{"name": f.name, "arguments": f.arguments} for f in fcalls]}
        items += [o.model_dump(exclude_none=True) for o in r.output]
        if not fcalls:
            return AgentResult(r.output_text or "", step, calls, items)
        for fc in fcalls:
            try:
                args = json.loads(fc.arguments or "{}")
            except json.JSONDecodeError:
                args, out, err = {}, json.dumps({"error": "arguments were not valid JSON"}), True
            else:
                tool = by_name.get(fc.name)
                out, err = (tool.run(args) if tool else (json.dumps({"error": f"unknown tool {fc.name}"}), True))
            calls.append({"tool": fc.name, "args": args, "error": err, "result": _loads(out)})
            items.append({"type": "function_call_output", "call_id": fc.call_id, "output": out})
    items.append({"role": "user", "content": "Step limit reached. Stop calling tools and give your final answer now."})
    with span("llm", f"{name}:final", input={"n_items": len(items)}, prompt_version=prompt_version) as s:
        r = client.responses.create(model=model, instructions=system, input=items, **extra)
        s.add_usage(model, r.usage.input_tokens, r.usage.output_tokens)
        s.output = {"text": r.output_text}
    return AgentResult(r.output_text or "", max_steps, calls, items)


def _anthropic_loop(name: str, system: str, user: str, tools: list[Tool], model: str, max_steps: int,
                    prompt_version: str, history: list[dict]) -> AgentResult:
    client = _client("anthropic")
    by_name = {t.name: t for t in tools}
    messages: list[Any] = [*history, {"role": "user", "content": user}]
    calls: list[dict] = []
    for step in range(1, max_steps + 1):
        with span("llm", f"{name}:turn{step}", input={"n_messages": len(messages)}, prompt_version=prompt_version) as s:
            r = client.messages.create(model=model, max_tokens=16000, system=system, messages=messages,
                                       tools=[t.anthropic_spec() for t in tools])
            s.add_usage(model, r.usage.input_tokens, r.usage.output_tokens)
            s.output = {"stop_reason": r.stop_reason, "content": [b.model_dump() for b in r.content]}
        messages.append({"role": "assistant", "content": r.content})
        uses = [b for b in r.content if b.type == "tool_use"]
        if r.stop_reason != "tool_use" or not uses:
            return AgentResult("".join(b.text for b in r.content if b.type == "text"), step, calls, messages)
        results = []
        for b in uses:
            tool = by_name.get(b.name)
            out, err = tool.run(b.input) if tool else (json.dumps({"error": f"unknown tool {b.name}"}), True)
            calls.append({"tool": b.name, "args": b.input, "error": err, "result": _loads(out)})
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": err})
        messages.append({"role": "user", "content": results})
    return AgentResult("Step limit reached.", max_steps, calls, messages)
