import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_http(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except Exception as exc:
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"viewer did not start at {url}: {last_error}")


def write_pgn(path: Path, slug: str, white: str, black: str, result: str, body: str, live: bool) -> None:
    headers = [
        f'[Event "{slug}"]',
        '[Site "C:/dev/chess-harness-codex"]',
        '[Date "2026.05.12"]',
        '[Round "1"]',
        f'[White "{white}"]',
        f'[Black "{black}"]',
        f'[Result "{result}"]',
    ]
    if live:
        headers.extend(
            [
                '[WhiteClockMs "300000"]',
                '[BlackClockMs "300000"]',
                f'[ClockUpdatedAtEpochMs "{int(time.time() * 1000)}"]',
                '[ClockRunningSide "White"]',
            ]
        )
    else:
        headers.extend(
            [
                '[Termination "Timeout stale game"]',
                '[WhiteClockMs "0"]',
                '[BlackClockMs "180000"]',
                '[ClockUpdatedAtEpochMs "100000"]',
                '[ClockRunningSide ""]',
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(headers + ["", body, ""]), encoding="utf-8")


class LiveViewerHashE2ETests(unittest.TestCase):
    def test_stale_newer_bare_hash_follows_older_active_live_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            live_dir = out_dir / "live"
            stale_slug = "codex-vs-codex-learner-live-20260512-181557"
            active_slug = "codex-vs-codex-learner-live-20260512-153641"
            stale_pgn = live_dir / f"{stale_slug}-live.pgn"
            active_pgn = live_dir / f"{active_slug}-live.pgn"
            write_pgn(stale_pgn, stale_slug, "StaleWhite", "StaleBlack", "0-1", "1. e4 e5 0-1", live=False)
            write_pgn(active_pgn, active_slug, "ActiveWhite", "ActiveBlack", "*", "1. d4 d5 *", live=True)
            active_pgn.with_suffix(".status.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-12 18:30:00",
                        "generated_at_epoch": time.time(),
                        "output_pgn": str(active_pgn),
                        "locked_game": 1,
                        "games": [
                            {
                                "game": 1,
                                "total": 2,
                                "white": "ActiveWhite",
                                "black": "ActiveBlack",
                                "result": "*",
                                "reason": "",
                                "finished": False,
                            },
                            {
                                "game": 2,
                                "total": 2,
                                "white": "SecondWhite",
                                "black": "SecondBlack",
                                "result": "*",
                                "reason": "",
                                "finished": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            port = free_port()
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "tools" / "live_pgn_viewer.py"),
                    "--pgn",
                    str(stale_pgn),
                    "--stats-dir",
                    str(out_dir),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--no-analysis",
                ],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                wait_for_http(f"http://127.0.0.1:{port}/")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1280, "height": 900})
                    page.goto(f"http://127.0.0.1:{port}/#{stale_slug}", wait_until="domcontentloaded")
                    page.wait_for_function(
                        "slug => window.location.hash === '#' + slug + '--live-game-1'",
                        arg=active_slug,
                        timeout=10000,
                    )
                    page.wait_for_function(
                        "slug => document.querySelector('#tournament-chip')?.textContent.includes(slug)",
                        arg=active_slug,
                        timeout=10000,
                    )
                    self.assertIn(active_slug, page.locator("#tournament-chip").inner_text())
                    self.assertEqual(page.locator("#current-game-title").inner_text(), "Game 1: ActiveWhite vs ActiveBlack")
                    self.assertIn("White: ActiveWhite", page.locator("#white-player").inner_text())
                    self.assertIn("Black: ActiveBlack", page.locator("#black-player").inner_text())
                    self.assertEqual(page.locator(".sq").count(), 64)
                    page.locator(".match-row.in-progress", has_text="SecondWhite").locator(".match-select").click()
                    page.wait_for_function(
                        "slug => window.location.hash === '#' + slug + '--live-game-2'",
                        arg=active_slug,
                        timeout=10000,
                    )
                    browser.close()
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
