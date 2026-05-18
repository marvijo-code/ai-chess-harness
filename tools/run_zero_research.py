from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ZERO_RESEARCH_PATH = ROOT / "engines" / "codex-chess-zero" / "zero_research.py"


def load_zero_research():
    spec = importlib.util.spec_from_file_location("codex_chess_zero_research_cli", ZERO_RESEARCH_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {ZERO_RESEARCH_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    load_zero_research().main()
