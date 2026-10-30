"""Settings. Everything is overridable with COMPDOC_* environment variables (or .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _env(name: str, default: str) -> str:
    return os.environ.get(f"COMPDOC_{name}", default)


@dataclass(frozen=True)
class Settings:
    # provider: openai | anthropic | offline. "offline" runs every agent with rule-based policies that call
    # the same tools - useful for tests, demos without credits, and as an extraction baseline.
    provider: str = field(default_factory=lambda: _env("PROVIDER", "openai"))
    agent_model: str = field(default_factory=lambda: _env("AGENT_MODEL", "gpt-5.5"))
    fast_model: str = field(default_factory=lambda: _env("FAST_MODEL", "gpt-5.4-mini"))
    max_agent_steps: int = field(default_factory=lambda: int(_env("MAX_AGENT_STEPS", "14")))

    var_dir: Path = field(default_factory=lambda: Path(_env("VAR_DIR", str(ROOT / "var"))))
    source_dir: Path = field(default_factory=lambda: Path(_env("SOURCE_DIR", str(ROOT / "data" / "synth" / "out"))))

    redact_spans: bool = field(default_factory=lambda: _env("REDACT_SPANS", "0") == "1")
    ocr_engine: str = field(default_factory=lambda: _env("OCR_ENGINE", "ocrmac"))  # ocrmac | easyocr | tesseract

    @property
    def db_path(self) -> Path:
        return self.var_dir / "compdoc.db"

    @property
    def checkpoint_path(self) -> Path:
        return self.var_dir / "checkpoints.db"

    @property
    def cache_dir(self) -> Path:
        return self.var_dir / "cache"

    @property
    def drafts_dir(self) -> Path:
        return self.var_dir / "drafts"


settings = Settings()
for d in (settings.var_dir, settings.cache_dir, settings.drafts_dir):
    d.mkdir(parents=True, exist_ok=True)

# USD per 1M tokens (input, output). Used only for cost metrics in traces; adjust to your contract.
PRICING = {
    "gpt-5.5": (1.25, 10.0),
    "gpt-5.4-mini": (0.25, 2.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "offline": (0.0, 0.0),
}
