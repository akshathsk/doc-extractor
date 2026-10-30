"""Tests run offline (no LLM) against a temporary var dir, using the synthetic contracts."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["COMPDOC_PROVIDER"] = "offline"
os.environ["COMPDOC_VAR_DIR"] = tempfile.mkdtemp(prefix="compdoc-test-")
if not (ROOT / "data/synth/out/manifest.json").exists():
    subprocess.run([sys.executable, str(ROOT / "data/synth/generate.py")], check=True)
