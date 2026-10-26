"""Run a series with a self-review between games: the player may rewrite its own memory and tools.

Use it to test whether an agent actually improves when it is allowed to learn: between
games it sees the finished game, its memory, its notes and its tools, and may rewrite any
of them. Tools go through the shared policy (python-chess only, no engine, no network).
The only thing that changes between games is what the agent itself wrote.

After each game the model is shown the game, its MEMORY.md, its notes and the
tools it currently has. It replies with JSON file edits. Tools are validated by
the shared policy (python-chess only, no engine, no network) and the memory
edits by the memory validator. The next game uses whatever it wrote, so a change
in its play is attributable to its own learning.
"""
import importlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_chess_memory as mem          # noqa: E402
import player_tools as ptools          # noqa: E402
series_mod = importlib.import_module("tools.play_llm_series")

ROOT = Path(__file__).resolve().parents[1]
AGENT = Path(os.environ.get("REVIEW_AGENT_DIR") or (ROOT / "out" / "agents" / "deepseek1017"))
TOOLS = AGENT / "tools"
MEMORY = AGENT / "MEMORY.md"
API = "https://opencode.ai/zen/go/v1/chat/completions"
MODEL = "deepseek-v4.1-flash"

DEEPSEEK = {"name": "DeepSeek V4.1 Flash (OC-Go)", "provider": "opencode-go",
            "model": MODEL, "effort": "high"}
STOCKFISH = {"name": "Stockfish 19 (depth 4)", "provider": "uci", "model": "stockfish-19",
             "effort": "depth 4", "depth": 4,
             "path": "out/engines/stockfish-19/stockfish/stockfish-windows-x86-64-universal.exe",
             "options": {"Threads": 1, "Hash": 16}}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def ask(prompt: str, timeout: int = 420) -> str:
    key = os.environ["OPENCODE_GO_API_KEY"]
    payload = {"model": MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 8000}
    req = urllib.request.Request(API, data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "x-opencode-session": "self-review"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read().decode())
    choices = data.get("choices") or [{}]
    return (choices[0].get("message") or {}).get("content") or ""


def review_prompt(game_text: str, summary: str) -> str:
    notes = sorted(p.name for p in (AGENT / "notes").glob("*.md")) if (AGENT / "notes").is_dir() else []
    tool_names = sorted(p.stem for p in ptools.discover(TOOLS).values())
    tool_text = "\n\n".join(f"--- tools/{n}.py ---\n{read(TOOLS / (n + '.py'))}" for n in tool_names) or "(none)"
    return f"""You are DeepSeek playing Stockfish depth 4. A game just ended.

GAME
{game_text}

HOW IT ENDED
{summary}

WHAT YOU MAY WRITE
- MEMORY.md: your index of lessons (shown with every move). Cap {mem.MEMORY_MAX_BYTES} bytes.
- notes/<topic>.md: optional topic notes (not shown during play). Cap {mem.NOTE_MAX_BYTES} bytes each.
- tools/<name>.py: your own verification tools, run by the harness before every move of your NEXT games.
  {ptools.TOOL_CONTRACT}
  At most {ptools.MAX_TOOL_FILES} tools, {ptools.TOOL_MAX_BYTES} bytes each.

YOUR CURRENT MEMORY.md
{read(MEMORY).strip() or "(empty)"}

YOUR CURRENT TOOLS
{tool_text}

YOUR NOTES
{chr(10).join(f"--- notes/{n} ---{chr(10)}{read(AGENT / 'notes' / n)}" for n in notes) or "(none)"}

Think about what actually decided this game. If a check you keep missing can be computed, write or
sharpen a tool for it instead of repeating a sentence. Keep every file short.

Reply with ONLY this JSON:
{{"summary": "<what decided the game>", "edits": [{{"path": "MEMORY.md", "content": "<full file>"}},
{{"path": "tools/<name>.py", "content": "<full file>"}}, {{"path": "notes/<topic>.md", "content": "<full file>"}}]}}"""


def apply_edits(text: str) -> list[str]:
    data, why = mem.parse_reflection(text)
    if data is None:
        return [f"reflection not parsed: {why}"]
    edits = data.get("edits") or []
    current = {f"tools/{p.name}": read(p) for p in TOOLS.glob("*.py")} if TOOLS.is_dir() else {}
    for p in (AGENT / "notes").glob("*.md") if (AGENT / "notes").is_dir() else []:
        current[f"notes/{p.name}"] = read(p)
    current["MEMORY.md"] = read(MEMORY)
    existing_notes = sorted(k.split("/", 1)[1] for k in current if k.startswith("notes/"))
    accepted, rejected = mem.validate_edits(edits, existing_notes, current=current)
    for reason in rejected:
        log(f"  edit rejected: {reason}")
    for edit in accepted:
        path = AGENT / edit["path"]
        if edit.get("delete"):
            path.unlink(missing_ok=True)
            log(f"  deleted {edit['path']}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(edit["content"], encoding="utf-8")
        log(f"  wrote {edit['path']} ({len(edit['content'])} bytes)")
    log(f"  summary: {str(data.get('summary'))[:200]}")
    return [f"applied {len(accepted)} edit(s)"]


def main() -> int:
    AGENT.mkdir(parents=True, exist_ok=True)
    TOOLS.mkdir(parents=True, exist_ok=True)
    if not MEMORY.exists():
        MEMORY.write_text("# DeepSeek memory\n\n(no lessons yet)\n", encoding="utf-8")

    games = int(os.environ.get("SERIES_GAMES", "3"))
    slug = time.strftime("deepseek-tools-series-vs-sf4-%Y%m%d-%H%M%S")
    cfg = series_mod.load_match_config()
    cfg.update({"games": games, "playAll": True, "timeControlMs": 1_200_000, "incrementMs": 10_000,
                "maxAttempts": 3, "attemptTimeoutSeconds": 900, "maxPlies": 400, "showLegalMoves": True})

    live = series_mod.LIVE_DIR / f"{slug}-live.pgn"
    series = {"id": slug, "games": games, "player1": DEEPSEEK["name"], "player2": STOCKFISH["name"],
              "score": {"player1": 0.0, "player2": 0.0}, "current_game": 1, "finished": False, "winner": None}
    writer = series_mod.SeriesWriter(live, series)
    series_mod.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    log(f"series {slug}: {games} games, review between each")

    player = dict(DEEPSEEK, toolsDir=str(TOOLS))
    engines = {1: series_mod.LlmEngine(player, cfg), 2: series_mod.LlmEngine(dict(STOCKFISH), cfg)}
    results = []
    try:
        for number in range(1, games + 1):
            series["current_game"] = number
            white_key, black_key = (1, 2) if number % 2 else (2, 1)
            summary = series_mod.play_game(number, engines[white_key], engines[black_key], cfg, series, writer, log)
            results.append(summary)
            log(f"game {number}: {summary['result']} ({summary['termination']})")
            if number == games:
                break
            game_text = str(writer.games[-1])
            said = ask(review_prompt(game_text, f"{summary['result']} ({summary['termination']})"))
            log("review returned, applying")
            apply_edits(said)
            log(f"tools now: {sorted(p.stem for p in ptools.discover(TOOLS).values())}")
            # New game, new engines: the next game must load the rewritten tools.
            for key in (1, 2):
                engines[key].close()
            engines = {1: series_mod.LlmEngine(dict(player), cfg), 2: series_mod.LlmEngine(dict(STOCKFISH), cfg)}
        series["finished"] = True
    finally:
        for engine in engines.values():
            engine.close()

    pgn = series_mod.ARCHIVE_DIR / f"{slug}.pgn"
    series_mod.write_text_retry(pgn, "\n\n".join(str(g) for g in writer.games) + "\n\n")
    (series_mod.ARCHIVE_DIR / f"{slug}.json").write_text(json.dumps(
        {"series": series, "config": cfg, "players": [player, STOCKFISH], "games": results,
         "live_pgn": str(live), "archive_pgn": str(pgn)}, indent=2), encoding="utf-8")
    log(f"series finished: {[(r['result'], r['termination']) for r in results]}")
    log(f"archive: {pgn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
