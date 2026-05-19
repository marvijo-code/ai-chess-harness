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


def read_sse_event_names(url: str, expected_count: int = 4, timeout: float = 5.0) -> list[str]:
    deadline = time.time() + timeout
    events: list[str] = []
    with urllib.request.urlopen(url, timeout=timeout) as response:
        while time.time() < deadline and len(events) < expected_count:
            line = response.readline().decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("event: "):
                events.append(line.removeprefix("event: ").strip())
    return events


def write_pgn(
    path: Path,
    slug: str,
    white: str,
    black: str,
    result: str,
    body: str,
    live: bool,
    round_name: str = "1",
    total_games: int | None = None,
) -> None:
    headers = [
        f'[Event "{slug}"]',
        '[Site "C:/dev/chess-harness-codex"]',
        '[Date "2026.05.12"]',
        f'[Round "{round_name}"]',
        f'[White "{white}"]',
        f'[Black "{black}"]',
        f'[Result "{result}"]',
    ]
    if total_games is not None:
        headers.append(f'[TotalGames "{total_games}"]')
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


def completed_game_text(slug: str, round_name: str, white: str, black: str, result: str, body: str) -> str:
    headers = [
        f'[Event "{slug}"]',
        '[Site "C:/dev/chess-harness-codex"]',
        '[Date "2026.05.12"]',
        f'[Round "{round_name}"]',
        f'[White "{white}"]',
        f'[Black "{black}"]',
        f'[Result "{result}"]',
        '[TotalGames "2"]',
        '[Termination "normal"]',
    ]
    return "\n".join(headers + ["", body, ""])


class LiveViewerHashE2ETests(unittest.TestCase):
    def test_zero_depth_live_hash_loads_active_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            slug = "zero-vs-stockfish-depth-1-20260519-101500"
            live_pgn = out_dir / "live" / f"{slug}-live.pgn"
            write_pgn(
                live_pgn,
                slug,
                "Codex-chess-zero",
                "Stockfish depth 1",
                "*",
                "1. d4 *",
                live=True,
                round_name="1",
                total_games=1,
            )
            live_pgn.with_suffix(".status.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-19 10:15:00",
                        "generated_at_epoch": time.time(),
                        "output_pgn": str(live_pgn),
                        "control_pgn": str(live_pgn),
                        "locked_game": 1,
                        "games": [
                            {
                                "game": 1,
                                "total": 1,
                                "white": "Codex-chess-zero",
                                "black": "Stockfish depth 1",
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
                    str(live_pgn),
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
                    page.route("**/api/stats?**", lambda route: route.abort())
                    page.goto(f"http://127.0.0.1:{port}/#{slug}--live-game-1", wait_until="domcontentloaded")
                    page.wait_for_function(
                        "() => document.querySelector('#current-game-title')?.textContent === 'Game 1 / 1: Codex-chess-zero vs Stockfish depth 1'",
                        timeout=10000,
                    )
                    self.assertEqual(page.evaluate("window.location.hash"), f"#{slug}--live-game-1")
                    self.assertTrue(page.locator("#follow-toggle").is_checked())
                    self.assertIn(page.locator("#status-text").inner_text(), {"Updated", "Watching"})
                    self.assertIn(slug, page.locator("#tournament-chip").inner_text())
                    self.assertEqual(page.locator(".sq").count(), 64)
                    browser.close()
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

    def test_archive_hash_loads_game_when_default_pgn_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            slug = "zero-vs-stockfish-depth-1-20260519-archive"
            archive_pgn = out_dir / "zero-depth-matches" / f"{slug}.pgn"
            write_pgn(
                archive_pgn,
                slug,
                "Codex-chess-zero",
                "Stockfish depth 1",
                "0-1",
                "1. d4 d5 0-1",
                live=False,
            )
            missing_default = out_dir / "live" / "missing-default-live.pgn"

            port = free_port()
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "tools" / "live_pgn_viewer.py"),
                    "--pgn",
                    str(missing_default),
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
                    page.goto(f"http://127.0.0.1:{port}/#{slug}--game-1", wait_until="domcontentloaded")
                    page.wait_for_function(
                        "() => document.querySelector('#current-game-title')?.textContent === 'Game 1: Codex-chess-zero vs Stockfish depth 1'",
                        timeout=10000,
                    )
                    self.assertEqual(page.locator("#status-text").inner_text(), "Archive")
                    self.assertIn(slug, page.locator("#tournament-chip").inner_text())
                    self.assertIn("White: Codex-chess-zero", page.locator("#white-player").inner_text())
                    self.assertIn("Black: Stockfish depth 1", page.locator("#black-player").inner_text())
                    self.assertEqual(page.locator(".sq").count(), 64)
                    browser.close()
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()

    def test_push_stream_replaces_fixed_game_stats_and_learner_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            slug = "codex-vs-codex-learner-live-20260514-push"
            live_pgn = out_dir / "live" / f"{slug}-live.pgn"
            write_pgn(live_pgn, slug, "PushWhite", "PushBlack", "*", "1. e4 e5 *", live=True, round_name="1", total_games=1)

            port = free_port()
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "tools" / "live_pgn_viewer.py"),
                    "--pgn",
                    str(live_pgn),
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
                base_url = f"http://127.0.0.1:{port}"
                wait_for_http(f"{base_url}/")
                events = set(read_sse_event_names(f"{base_url}/api/events", expected_count=5))
                self.assertTrue({"game", "stats", "learner", "research", "viewer-version"}.issubset(events))

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1280, "height": 900})
                    page.add_init_script(
                        """
                        window.__fetchLog = [];
                        const originalFetch = window.fetch.bind(window);
                        window.fetch = (input, init) => {
                          const url = typeof input === "string" ? input : (input && input.url) || "";
                          if (url.startsWith("/api/")) window.__fetchLog.push({ url, ts: Date.now() });
                          return originalFetch(input, init);
                        };
                        """
                    )
                    page.goto(f"{base_url}/", wait_until="domcontentloaded")
                    page.wait_for_function("() => window.__viewerPushConnected === true", timeout=10000)
                    page.wait_for_timeout(1500)
                    marker = page.evaluate("() => Date.now()")
                    page.wait_for_timeout(3600)
                    recurring = page.evaluate(
                        """marker => window.__fetchLog
                          .filter(item => item.ts > marker)
                          .map(item => item.url)
                          .filter(url => url.startsWith("/api/game") || url.startsWith("/api/stats") || url.startsWith("/api/learner") || url.startsWith("/api/viewer-version"))""",
                        marker,
                    )
                    self.assertEqual(recurring, [])
                    browser.close()
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

    def test_selecting_older_archive_game_after_replay_moves_board_to_that_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            slug = "codex-vs-codex-learner-standard5-20260514-archive"
            archive_pgn = out_dir / "fastchess" / f"{slug}.pgn"
            archive_pgn.parent.mkdir(parents=True, exist_ok=True)
            archive_pgn.write_text(
                "\n".join(
                    [
                        completed_game_text(slug, "1", "OldWhite", "OldBlack", "1-0", "1. e4 e5 1-0"),
                        completed_game_text(slug, "2", "NewWhite", "NewBlack", "0-1", "1. d4 d5 0-1"),
                    ]
                ),
                encoding="utf-8",
            )

            port = free_port()
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "tools" / "live_pgn_viewer.py"),
                    "--pgn",
                    str(archive_pgn),
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
                    page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
                    page.wait_for_function(
                        "() => document.querySelector('#current-game-title')?.textContent === 'Game 2 / 2: NewWhite vs NewBlack'",
                        timeout=10000,
                    )
                    self.assertEqual(page.locator("#current-game-title").inner_text(), "Game 2 / 2: NewWhite vs NewBlack")
                    page.keyboard.press("ArrowLeft")
                    page.wait_for_function("() => !document.querySelector('#follow-toggle')?.checked", timeout=10000)
                    self.assertEqual(page.evaluate("window.location.hash"), f"#{slug}--game-2")
                    page.locator(".match-row", has_text="OldWhite").locator(".match-select").click()
                    page.wait_for_function(
                        "slug => window.location.hash === '#' + slug + '--game-1'",
                        arg=slug,
                        timeout=10000,
                    )
                    page.wait_for_function(
                        "() => document.querySelector('#current-game-title')?.textContent === 'Game 1 / 2: OldWhite vs OldBlack'",
                        timeout=10000,
                    )
                    self.assertIn("White: OldWhite", page.locator("#white-player").inner_text())
                    page.keyboard.press("ArrowLeft")
                    page.wait_for_timeout(1500)
                    self.assertEqual(page.evaluate("window.location.hash"), f"#{slug}--game-1")
                    self.assertEqual(page.locator("#current-game-title").inner_text(), "Game 1 / 2: OldWhite vs OldBlack")
                    self.assertIn("White: OldWhite", page.locator("#white-player").inner_text())
                    browser.close()
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

    def test_stale_newer_bare_hash_follows_older_active_live_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            live_dir = out_dir / "live"
            stale_slug = "codex-vs-codex-learner-live-20260512-181557"
            active_slug = "codex-vs-codex-learner-live-20260512-153641"
            stale_pgn = live_dir / f"{stale_slug}-live.pgn"
            active_pgn = live_dir / f"{active_slug}-live.pgn"
            write_pgn(stale_pgn, stale_slug, "StaleWhite", "StaleBlack", "0-1", "1. e4 e5 0-1", live=False)
            write_pgn(active_pgn, active_slug, "ActiveWhite", "ActiveBlack", "*", "1. d4 d5 *", live=True, round_name="7", total_games=7)
            live_games = [
                {
                    "game": game_no,
                    "total": 7,
                    "white": f"OtherWhite{game_no}",
                    "black": f"OtherBlack{game_no}",
                    "result": "*",
                    "reason": "",
                    "finished": False,
                }
                for game_no in range(1, 7)
            ]
            live_games.append(
                {
                    "game": 7,
                    "total": 7,
                    "white": "ActiveWhite",
                    "black": "ActiveBlack",
                    "result": "*",
                    "reason": "",
                    "finished": False,
                }
            )
            active_pgn.with_suffix(".status.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-12 18:30:00",
                        "generated_at_epoch": time.time(),
                        "output_pgn": str(active_pgn),
                        "locked_game": 7,
                        "games": live_games,
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
                    page.goto(f"http://127.0.0.1:{port}/#{active_slug}--live-game-7", wait_until="domcontentloaded")
                    page.wait_for_function(
                        "slug => window.location.hash === '#' + slug + '--live-game-7'",
                        arg=active_slug,
                        timeout=10000,
                    )
                    page.wait_for_function(
                        "slug => document.querySelector('#tournament-chip')?.textContent.includes(slug)",
                        arg=active_slug,
                        timeout=10000,
                    )
                    self.assertIn(active_slug, page.locator("#tournament-chip").inner_text())
                    self.assertEqual(page.locator("#current-game-title").inner_text(), "Game 7 / 7: ActiveWhite vs ActiveBlack")
                    self.assertIn("White: ActiveWhite", page.locator("#white-player").inner_text())
                    self.assertIn("Black: ActiveBlack", page.locator("#black-player").inner_text())
                    self.assertEqual(page.locator(".sq").count(), 64)
                    page.keyboard.press("ArrowLeft")
                    page.wait_for_function(
                        "slug => window.location.hash === '#' + slug + '--live-game-7'",
                        arg=active_slug,
                        timeout=10000,
                    )
                    self.assertFalse(page.locator("#follow-toggle").is_checked())
                    self.assertNotIn("--game-7", page.evaluate("window.location.hash"))
                    self.assertIn(active_slug, page.locator("#tournament-chip").inner_text())
                    self.assertIn(active_slug, page.locator("#pgn-path").inner_text())
                    self.assertEqual(page.locator("#current-game-title").inner_text(), "Game 7 / 7: ActiveWhite vs ActiveBlack")
                    self.assertIn("White: ActiveWhite", page.locator("#white-player").inner_text())
                    page.wait_for_timeout(4000)
                    self.assertFalse(page.locator("#follow-toggle").is_checked())
                    self.assertEqual(page.evaluate("window.location.hash"), f"#{active_slug}--live-game-7")
                    page.locator(".match-row.in-progress", has_text="OtherWhite6").locator(".match-select").click()
                    page.wait_for_function(
                        "slug => window.location.hash === '#' + slug + '--live-game-6'",
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

    def test_viewer_explicit_live_game_hash_stays_pinned_after_completion_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            live_dir = out_dir / "live"
            pinned_slug = "codex-vs-codex-learner-live-20260518-125000"
            latest_slug = "codex-vs-codex-learner-live-20260518-125005"
            pinned_pgn = live_dir / f"{pinned_slug}-game-8-live.pgn"
            latest_pgn = live_dir / f"{latest_slug}-game-10-live.pgn"
            write_pgn(
                pinned_pgn,
                pinned_slug,
                "PinnedWhite",
                "PinnedBlack",
                "1/2-1/2",
                "1. e4 e5 1/2-1/2",
                live=False,
                round_name="8",
                total_games=100,
            )
            write_pgn(
                latest_pgn,
                latest_slug,
                "LatestWhite",
                "LatestBlack",
                "*",
                "1. d4 d5 *",
                live=True,
                round_name="10",
                total_games=100,
            )
            (live_dir / "pinned-live.status.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-18 12:50:00",
                        "generated_at_epoch": time.time(),
                        "output_pgn": str(pinned_pgn),
                        "control_pgn": str(live_dir / "run-live.pgn"),
                        "locked_game": 8,
                        "games": [
                            {
                                "game": 8,
                                "total": 100,
                                "white": "PinnedWhite",
                                "black": "PinnedBlack",
                                "result": "1/2-1/2",
                                "reason": "Draw by 3-fold repetition",
                                "finished": True,
                            },
                            {
                                "game": 10,
                                "total": 100,
                                "white": "LatestWhite",
                                "black": "LatestBlack",
                                "result": "*",
                                "reason": "",
                                "finished": False,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (live_dir / "latest-live.status.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-18 12:50:05",
                        "generated_at_epoch": time.time() + 5,
                        "output_pgn": str(latest_pgn),
                        "control_pgn": str(live_dir / "run-live.pgn"),
                        "locked_game": 10,
                        "games": [
                            {
                                "game": 10,
                                "total": 100,
                                "white": "LatestWhite",
                                "black": "LatestBlack",
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
                    str(latest_pgn),
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
                    page.goto(f"http://127.0.0.1:{port}/#{pinned_slug}--live-game-8", wait_until="domcontentloaded")
                    page.wait_for_function(
                        "slug => window.location.hash === '#' + slug + '--live-game-8'",
                        arg=pinned_slug,
                        timeout=10000,
                    )
                    page.wait_for_function(
                        "() => document.querySelector('#current-game-title')?.textContent === 'Game 8 / 100: PinnedWhite vs PinnedBlack'",
                        timeout=10000,
                    )
                    self.assertIn(pinned_slug, page.locator("#tournament-chip").inner_text())
                    page.keyboard.press("ArrowLeft")
                    page.wait_for_function("() => !document.querySelector('#follow-toggle')?.checked", timeout=10000)
                    page.wait_for_timeout(2500)
                    self.assertEqual(page.evaluate("window.location.hash"), f"#{pinned_slug}--live-game-8")
                    self.assertEqual(page.locator("#current-game-title").inner_text(), "Game 8 / 100: PinnedWhite vs PinnedBlack")
                    self.assertIn(pinned_slug, page.locator("#pgn-path").inner_text())
                    browser.close()
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
