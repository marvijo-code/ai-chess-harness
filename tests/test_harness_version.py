"""The harness version must be bumped whenever the harness changes (owner 2026-10-16)."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engines/llm-chess-engine"))
import harness_version as hv


def test_the_version_was_bumped_for_the_current_sources():
    assert hv.sources_sha256() == hv.SOURCES_SHA256, (
        f"harness sources changed but HARNESS_VERSION is still {hv.HARNESS_VERSION}: "
        "run python tools/bump_harness_version.py")


def test_every_engine_module_is_fingerprinted():
    engine = sorted(f"engines/llm-chess-engine/{p.name}" for p in (ROOT / "engines/llm-chess-engine").glob("*.py")
                    if p.name != "harness_version.py")
    assert set(engine) <= set(hv.SOURCES)


def test_line_endings_do_not_change_the_fingerprint(tmp_path):
    for rel in hv.SOURCES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((ROOT / rel).read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert hv.sources_sha256(tmp_path) == hv.sources_sha256()


def test_engine_reports_its_version_in_the_uci_handshake():
    out = subprocess.run([sys.executable, str(ROOT / "engines/llm-chess-engine/llm_chess_uci.py")],
                         input="uci\nquit\n", capture_output=True, text=True, timeout=60).stdout
    line = next(line for line in out.splitlines() if line.startswith("id name "))
    assert hv.engine_version(line[len("id name "):]) == hv.HARNESS_VERSION


def test_other_engines_have_no_harness_version():
    assert hv.engine_version("Stockfish 19") is None
    assert hv.engine_version("llm-chess-engine v7") == 7
