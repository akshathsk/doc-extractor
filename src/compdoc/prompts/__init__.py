"""Prompts live in prompts/*.md. The version is a content hash, stored on every LLM span and enrichment row,
so changing a prompt is visible in the Dev Console and triggers re-enrichment."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

DIR = Path(__file__).parent


@lru_cache
def load(name: str) -> tuple[str, str]:
    text = (DIR / f"{name}.md").read_text()
    return text, f"{name}@{hashlib.sha1(text.encode()).hexdigest()[:8]}"


def all_prompts() -> dict[str, tuple[str, str]]:
    return {p.stem: load(p.stem) for p in sorted(DIR.glob("*.md"))}
