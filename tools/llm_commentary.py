"""Spoken commentary for the live LLM tournament viewer, on subscriptions and a free voice.

A background thread watches the tournament state JSON. It roams the boards like a TV commentator:
each line goes to the board that matters most right now (tournament leaders playing, fresh blunders
or strong moves, checks, captures, time trouble, a result just in), with a nudge to rotate boards.
A board the viewer pins (focus mode) keeps the commentary. At most one line every MIN_GAP_SECONDS,
written from the recent moves, the players' own move comments and the Stockfish move marks, then
voiced. Clips are WAV files (24 kHz mono 16-bit) in out_dir; the viewer polls `clips()` and plays
them one at a time.

Routes (2026-10-08, owner: run the commentary 24/7 on the VPS with no metered spend):
  * Text: a subscription CLI or plan, never a metered API by default. COMMENTARY_ROUTE picks
    codex (default, `codex exec` on the ChatGPT login, gpt-6-luna at low effort, every tool off),
    claude (`claude -p` on the Claude login, haiku at low effort, no tools) or opencode-go (the
    OpenCode Go plan). The old OpenRouter path needs COMMENTARY_ROUTE=openrouter AND
    COMMENTARY_ALLOW_OPENROUTER=1 (off by default). COMMENTARY_MODEL / COMMENTARY_EFFORT override.
  * Voice: edge-tts (Microsoft neural voices, no key, no spend), COMMENTARY_VOICE picks the voice.
    The MP3 it returns is decoded to WAV with ffmpeg. A failed voice = that line stays silent.
    COMMENTARY_TTS=openrouter (needs the same opt-in) is the old metered Gemini voice.
  * Pacing: at most COMMENTARY_CALLS_PER_HOUR text calls (default 90) in any hour, spread by a
    small token bucket; thinking filler and routine updates stop at 75% of the cap so the big
    moments (results, recaps, previews) keep room. Subscription spend is 0: calls are counted.
  * Prompts are cache friendly: one byte-identical HOST_PROMPT first, the changing facts last.

The host is selective (owner 2026-10-06: "speed up boring times ... introduce openings, critical blunders and go to
winning/drawing endgames"): a board gets a line only for a reason (the opening named once, a critical blunder, an
endgame verdict once, a decided game once, a drawish stretch once, time trouble, a result, a leaders update every
so often). Stretches with nothing to say are reported as `quiet_s`; the viewer then runs a time-lapse tour of all
boards (the host is held during the tour and sums up what changed after it).

Every round opens with a spoken preview and closes with a recap that teases the next round. A new tournament id
(the runner starts the next tournament on its own; with `follow_dir` the newest state file is followed) resets
the per-tournament memory and opens with a fresh tournament intro. Stockfish's depth rises by one each time an AI
beats it: the host names the depth when Stockfish plays and announces each rise. Players keep notes; the host may
quote a player's latest note now and then.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from collections import deque
from pathlib import Path

try:
    import chess
except ImportError:  # the host still works, without openings/endgames/verdicts
    chess = None

API = "https://openrouter.ai/api/v1"
TEXT_MODEL = "google/gemini-3.1-flash-lite"      # openrouter route only (opt-in)
TTS_MODEL = "google/gemini-3.8-flash-tts"        # openrouter voice only (opt-in)
TTS_VOICE = "Puck"
TTS_STYLE = "Speak like an excited but clear chess commentator."
SAMPLE_RATE = 24000
MIN_GAP_SECONDS = 6.0
LEAD_SECONDS = 5.0        # openrouter: start writing the next line this long before the current one ends
CLI_LEAD_SECONDS = 10.0   # subscription CLIs take ~4-7 s to write and edge-tts ~1-2 s to voice
FILLER_GAP_SECONDS = 6.0  # quiet this long after a line and no new move: read a model's live thinking
RECAP_SECONDS = 600       # a round that ended longer ago than this gets no recap (resume after a pause)
INTRO_MAX_PLIES = 30      # a round further along than this gets no preview
LIVE_STATE_SECONDS = 240  # state older than this = paused or stopped: no round preview, no thinking lines
OPENING_PLIES = 8         # the opening is named once a board has this many half-moves
ENDGAME_MATERIAL = 26     # non-pawn, non-king material of both sides together (a full board is 62)
ENDGAME_MIN_PLIES = 24
DECIDED_CP = 450          # |score| this big for DECIDED_STREAK scores in a row = the game is decided
DECIDED_STREAK = 4
DRAW_CP = 35              # |score| this small for DRAW_STREAK scores in a row, late = heading for a draw
DRAW_STREAK = 12
DRAW_MIN_PLIES = 40
LEADER_GAP_PLIES = 10     # a top-two board gets an update after this many silent half-moves
UPDATE_GAP_PLIES = 20     # any live board gets an update after this many silent half-moves
BLUNDER_GAP_PLIES = 8     # blunders closer together than this are folded into one line (a blunder fest is one story)
TOUR_HOLD_MAX = 420.0     # a tour that never reports its end releases the host after this long
THINK_LINES = os.environ.get("COMMENTARY_THINK_LINES", "") in {"1", "true", "yes"}
PIECE_VALUE = {"n": 3, "b": 3, "r": 5, "q": 9}
REASON_SCORE = {"mate": 12.0, "blunder": 10.0, "mistake": 8.0, "opening": 7.0, "endgame": 7.0, "time": 6.0,
                "decided": 6.0, "draw": 4.0, "leaders": 3.0, "update": 2.0}
CRITICAL = {"mate", "blunder", "mistake", "time"}
LOW_PRIORITY_REASONS = {"leaders", "update"}     # dropped first when the hourly call cap gets close
NOTE_EVERY_LINES = 4      # at most one quoted player note per this many board lines
REASON_HINT = {
    "opening": "This line introduces the OPENING: name the opening or variation from the opening moves if you are sure "
               "(for example Sicilian Najdorf, Ruy Lopez), otherwise describe the setup in plain words, and say in a few "
               "words what each side is aiming for.",
    "blunder": "A CRITICAL MISTAKE just happened. Say clearly what went wrong and how big a swing it is.",
    "mistake": "A costly MISTAKE just happened. Say what went wrong.",
    "endgame": "The game has reached an ENDGAME. Say who is better (or that it is heading for a draw) and the plan.",
    "decided": "The game is now effectively DECIDED. Say who is winning and that it is only a matter of technique.",
    "draw": "The position is DRAWISH: nobody can make progress. Say it is heading for a draw.",
    "time": "TIME TROUBLE: a clock is almost out. Say who is short of time and what it means.",
    "mate": "The game ends in CHECKMATE. Say who won and how.",
}
# Only call while someone listens: an unmuted page (or the stream pusher) polls clips every 2 s (COMMENTARY_ALWAYS=1 overrides).
LISTENER_SECONDS = 20.0
DEFAULT_BUDGET_USD = 1.5  # metered (openrouter opt-in) routes only

# ---- routes ---------------------------------------------------------------------------------------
ROUTES = ("codex", "claude", "opencode-go", "openrouter")
METERED_ROUTES = {"openrouter"}
DEFAULT_ROUTE = "codex"
# Probed 2026-10-08 on the VPS: gpt-6-luna (smallest model the ChatGPT login lists) answered a line in ~6 s at
# low effort; claude haiku in ~3 s. Low is the lowest effort both CLIs accept.
DEFAULT_MODELS = {"codex": "gpt-6-luna", "claude": "haiku", "opencode-go": "deepseek-v4.1-flash", "openrouter": TEXT_MODEL}
DEFAULT_EFFORT = "low"
DEFAULT_CALLS_PER_HOUR = 90
DEFAULT_BURST = 6
CLI_TIMEOUT_SECONDS = 75
OPENCODE_GO_URL = "https://opencode.ai/zen/go/v1/chat/completions"   # /zen/go/ is the subscription path
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
# Same tool lock-down as the chess codex call (engines/llm-chess-engine/subscription_providers.py).
CODEX_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "unified_exec_tty", "apps", "browser_use", "browser_use_external", "computer_use",
    "in_app_browser", "multi_agent", "image_generation", "view_image", "memories", "plugins", "remote_plugin",
    "code_mode_host", "sleep_tool", "skill_search", "tool_suggest", "goals", "workspace_dependencies", "hooks",
)
CODEX_TOOL_ITEMS = ("command_execution", "file_change", "mcp_tool_call", "web_search", "patch_apply", "tool_call")
# Metered-credit keys must never leak into a subscription CLI from the parent shell.
STRIPPED_ENV = {
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"),
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"),
}
NPM_ROOT = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules"
WINDOWS_BINS = {
    "codex": NPM_ROOT / "@openai" / "codex" / "node_modules" / "@openai" / "codex-win32-x64" / "vendor"
             / "x86_64-pc-windows-msvc" / "bin" / "codex.exe",
    "claude": NPM_ROOT / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe",
}
LINUX_BINS = {"codex": ("/usr/bin/codex", "/usr/local/bin/codex"),
              "claude": (str(Path.home() / ".local" / "bin" / "claude"), "/usr/local/bin/claude", "/usr/bin/claude")}
UNAVAILABLE_MARKERS = ("usagelimit", "usage limit", "usage_limit", "rate limit", "insufficient balance",
                       "insufficient_quota", "exceeded your current quota", "credit balance", "payment required",
                       "not logged in", "please run /login", "invalid api key", "unauthorized", "http 401",
                       "http 402", "http 403", "http 429")
UNAVAILABLE_BACKOFF_SECONDS = 900   # plan limit or lost login: stop calling for 15 minutes
ERROR_BACKOFF_SECONDS = 30

# ---- voice ----------------------------------------------------------------------------------------
TTS_ROUTES = ("edge", "openrouter", "off")
# en-US-GuyNeural: the energetic, clear US newscaster voice, and fast to voice. Measured 2026-10-08 on the VPS
# with 3 fresh ~16 s lines each: Guy took 0.18x the clip length to voice (1.3-5.1 s), RyanNeural 0.16x (clear
# but calmer, British), AndrewMultilingualNeural 0.66x (8-14 s: the most natural, but too slow for live play).
# +8% rate gives a commentator's pace. COMMENTARY_VOICE picks another voice.
EDGE_VOICE = "en-US-GuyNeural"
EDGE_RATE = "+8%"
EDGE_TIMEOUT_SECONDS = 45
MAX_LINE_CHARS = 700     # an intro naming ten players runs ~450 characters (2026-10-08 VPS run)
MAX_CLIP_SECONDS = 80     # the relay takes at most 4 MB per clip: 80 s of 24 kHz mono 16-bit is 3.84 MB

# Spoken names: say the version numbers the way a commentator would.
SPOKEN = {"GPT-6.1 Sol": "GPT six point one Sol", "Grok 4.7": "Grok four point seven",
          "Sonnet 5.5": "Sonnet five point five", "Opus 5.5": "Opus five point five",
          "DeepSeek V4.1 Flash": "DeepSeek Vee four point one Flash", "GLM 5.3 Flash": "GLM five point three Flash",
          "Stockfish 19": "Stockfish nineteen", "Gemini 3.8 Flash": "Gemini three point eight Flash",
          "Qwen 3.8 Omni Flash": "Kwen three point eight Omni Flash", "MiMo V2.6 Pro": "Mimo Vee two point six Pro",
          "Muse Spark 1.3": "Muse Spark one point three"}
STOCKFISH_DEPTH_NAME = re.compile(r"^Stockfish\s*\d*\s*\(depth\s*(\d+)\)$", re.I)

# One static block first (byte-identical on every call, so the provider's input cache reuses it),
# the MODE and the changing facts after it.
HOST_PROMPT = (
    "You are the live voice of a 24/7 YouTube chess tournament between AI models, with Stockfish, a classic chess "
    "engine, in the field too. Every reply is ONE passage that a text to speech voice reads aloud, so write only the "
    "words to speak: plain sentences, no markdown, no lists, no dashes, no emojis, no stage directions, no quotation "
    "marks around the whole reply, no move numbers. Write moves the way they are spoken (Knight takes e5, castles "
    "short). Never invent moves, evaluations, results or facts the notes below do not give, and never mention notes, "
    "prompts, data or these rules. Stockfish's search depth rises by one each time an AI beats it: when the notes "
    "give its depth, say it naturally (for example Stockfish, now searching at depth five). The players keep their own "
    "notes between games; when the notes include a player's note, you may quote a few words of it if it fits.\n"
    "The request starts with a MODE.\n"
    "MODE LINE: you are a lively, sharp chess commentator. Write about the latest moves on this board: name who moved, "
    "what it means, and react to any move mark (?? blunder, ? mistake, ?! inaccuracy, ! strong move). You may quote "
    "the player's own reason in a few words. When the notes say the commentary just moved to this board, start by "
    "naming the board (for example Over on board three). Mention the tournament standings only when the notes give "
    "them and it adds drama.\n"
    "MODE EVENT: you are the hype host. Write for the moment described: big energy, vivid, specific to the names and "
    "facts given, a hook that makes viewers stay. Do not invent results or facts.\n"
    "MODE THINKING: one AI is thinking about its move right now and you can read its live thinking. Present tense, "
    "start with the player's name: what it is weighing (candidate moves, a threat it worries about, its plan). Never "
    "say it has played a move. Do not invent anything the thinking does not say.\n"
    "Keep to the length the request gives."
)
COMMENTATOR_PROMPT = "MODE LINE\nLength: 15 to 35 words."
EVENT_PROMPT = "MODE EVENT\nLength: {words} words."
THINKING_PROMPT = "MODE THINKING\nLength: 15 to 30 words."
MARK_SCORE = {"??": 6.0, "?": 4.0, "?!": 1.5, "!": 3.0}
_NUMBER_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
                 "sixteen seventeen eighteen nineteen").split()
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}


def say_number(n: int) -> str:
    if 0 <= n < 20:
        return _NUMBER_WORDS[n]
    if 20 <= n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + ("" if ones == 0 else " " + _NUMBER_WORDS[ones])
    return str(n)


def opening_event(state: dict, fresh: bool = False) -> dict | None:
    """The tournament intro: the field (Stockfish with its depth), the format, the stakes."""
    games = state.get("games") or {}
    live = [g for g in games.values() if g.get("status") == "live"]
    if not live:
        return None
    names = [player_label(state, p["name"]) for p in state.get("players") or [] if p.get("name")]
    fmt = state.get("format") or {}
    rr = fmt.get("rr_rounds")
    how = (f"a round robin of {rr} rounds where everyone plays everyone, then the top {fmt.get('ko_size', 4)} "
           "go to knockout semifinals and a final, and a drawn knockout game goes to an Armageddon decider") if rr else "a Swiss tournament"
    lead = ("A brand new tournament starts right after the last one ended. " if fresh else "The tournament is starting. ")
    sf = stockfish_depths(state)
    depth = (" Stockfish's search depth rises by one every time an AI beats it, so every win makes it harder."
             if sf else "")
    return {"key": "opening", "game": live[0]["id"], "words": "40 to 60",
            "facts": lead + f"{len(names)} players: {', '.join(names)}. Format: {how}." + depth
                     + " Only one of them will be crowned champion. Open the show."}


def next_event(state: dict, done: set) -> dict | None:
    """The next big moment to announce (opening hook, knockouts, an Armageddon decider, the final, the champion)."""
    games = state.get("games") or {}
    ko = state.get("knockout") or {}
    live = [g for g in games.values() if g.get("status") == "live"]
    started = any(r.get("status") == "finished" for r in state.get("rounds") or [])
    if "opening" not in done and live and not started and not any(g.get("result", "*") != "*" for g in games.values()):
        return opening_event(state)
    champion = ko.get("champion")
    if champion and "champion" not in done:
        final = next((m for m in ko.get("matches") or [] if m.get("id") == "final"), {})
        game_id = (final.get("games") or [None])[-1]
        return {"key": "champion", "game": game_id,
                "facts": f"The final is over. {spoken(champion)} is the champion, beating {spoken(ko.get('runner_up') or '?')} "
                         f"({final.get('decided_by') or 'game'}). Third place: {spoken(ko.get('third') or '?')}. "
                         "Crown the champion and close the show with a memorable outro that invites viewers to "
                         "say in the comments who they think should face the champion next."}
    for match in ko.get("matches") or []:
        for game_id in match.get("games") or []:
            game = games.get(game_id) or {}
            if game.get("armageddon") and game.get("status") == "live" and f"arm-{game_id}" not in done:
                return {"key": f"arm-{game_id}", "game": game_id,
                        "facts": f"{match['label']} was drawn, so it goes to an Armageddon decider: "
                                 f"{spoken(game['white'])} has White and more time, {spoken(game['black'])} has Black "
                                 "with less time but a draw sends Black through. Sudden death."}
    stage = state.get("stage")
    if stage == "semifinals" and "knockouts" not in done and ko.get("seeds"):
        seeds = ko["seeds"]
        sf = [m for m in ko.get("matches") or [] if m.get("stage") == "semifinals"]
        first = next((g for m in sf for g in m.get("games") or []), None)
        if first:
            return {"key": "knockouts", "game": first,
                    "facts": "The round robin is over. Knockouts begin. Seeds: "
                             + ", ".join(f"{s['seed']}. {spoken(s['name'])} with {s['points']:g} points" for s in seeds)
                             + ". Semifinal 1: seed 1 against seed 4. Semifinal 2: seed 2 against seed 3. Lose and you are out."}
    recap = _round_recap(state, done)
    if recap:
        return recap
    intro = _round_intro(state, done)
    if intro:
        return intro
    if stage == "final" and "final" not in done:
        final = next((m for m in ko.get("matches") or [] if m.get("id") == "final"), None)
        if final and final.get("games"):
            return {"key": "final", "game": final["games"][0],
                    "facts": f"The final starts: {spoken(final['a'])} against {spoken(final['b'])}. One game for the crown, "
                             "and an Armageddon decider if it is drawn."}
    return None
RECENT_RESULT_SECONDS = 180


def _end_epoch(game: dict) -> float | None:
    try:
        import datetime as dt

        return dt.datetime.fromisoformat(str(game.get("end"))).timestamp()
    except (TypeError, ValueError):
        return None


def _table(state: dict, limit: int = 10) -> list[dict]:
    rows = [r for r in state.get("standings") or [] if r.get("played")]
    return sorted(rows, key=lambda r: r.get("rank") or 99)[:limit]


def _record(row: dict) -> str:
    pts = row.get("points", 0)

    def n(count, word):
        return f"{count} {word}{'' if count == 1 else 'es' if word == 'loss' else 's'}"

    return (f"{row.get('rank', '?')}. {spoken(row['name'])} {n(pts, 'point') if pts != int(pts) else n(int(pts), 'point')} "
            f"({n(row.get('wins', 0), 'win')}, {n(row.get('draws', 0), 'draw')}, {n(row.get('losses', 0), 'loss')})")


def _rr_rounds(state: dict) -> list[dict]:
    return [r for r in state.get("rounds") or [] if not r.get("stage")]


def _round_intro(state: dict, done: set) -> dict | None:
    """Preview of a round robin round (2 and later; round 1 has the opening hook) as its games start."""
    games = state.get("games") or {}
    rnd = next((r for r in _rr_rounds(state) if r.get("round") == state.get("current_round")), None)
    if not rnd or rnd["round"] < 2 or f"round-{rnd['round']}" in done:
        return None
    rgames = [games.get(p.get("game_id")) or {} for p in rnd.get("pairings") or []]
    live = [g for g in rgames if g.get("status") == "live"]
    if not live or any(g.get("result", "*") != "*" for g in rgames):
        return None
    plies = max(len(g.get("moves") or []) for g in rgames)
    if plies < 1 or plies > INTRO_MAX_PLIES:
        return None  # not started (paired, then paused) or joined too late
    fmt = state.get("format") or {}
    rows = {r["name"]: r for r in _table(state, 99)}

    def who(name: str) -> str:
        r = rows.get(name)
        return f"{spoken(name)} (" + (f"rank {r.get('rank')}, {r.get('points', 0):g} points" if r else "no games yet") + ")"

    pairs = [f"Board {p.get('board')}: {who(p['white'])} with White against {who(p['black'])}"
             for p in rnd.get("pairings") or []]
    table = _table(state)
    rr = fmt.get("rr_rounds")
    cut = f" After round {rr} only the top {fmt.get('ko_size', 4)} go through to the knockouts." if rr else ""
    lead = sorted(live, key=lambda g: min(rows.get(g.get("white"), {}).get("rank") or 99,
                                          rows.get(g.get("black"), {}).get("rank") or 99))[0]
    sf = stockfish_depths(state)
    depth = "".join(f" {spoken(n)} searches at depth {d} this round." for n, d in sf.items())
    return {"key": f"round-{rnd['round']}", "game": lead["id"], "words": "40 to 60",
            "facts": f"Round {rnd['round']}" + (f" of {rr}" if rr else "") + " is starting." + cut
                     + " Standings now: " + "; ".join(_record(r) for r in table) + ". Pairings: " + "; ".join(pairs)
                     + "." + depth
                     + " Preview the round like a TV host: open with the biggest storyline (the leader, an unbeaten "
                       "run, someone still without a point, a revenge match), name the match of the round, and end "
                       "with a tease that makes viewers stay."}


def _round_recap(state: dict, done: set) -> dict | None:
    """Recap of the latest finished round robin round, with a tease of the next one."""
    games = state.get("games") or {}
    finished = [r for r in _rr_rounds(state) if r.get("status") == "finished"]
    if not finished:
        return None
    rnd = finished[-1]
    if f"recap-{rnd['round']}" in done:
        return None
    rgames = [games.get(p.get("game_id")) or {} for p in rnd.get("pairings") or []]
    ends = [(e, g) for g in rgames if (e := _end_epoch(g))]
    if not ends:
        return None
    last_end, last_game = max(ends, key=lambda t: t[0])
    if time.time() - last_end > RECAP_SECONDS:
        return None
    results = []
    for g in rgames:
        w, b, res = spoken(g.get("white", "?")), spoken(g.get("black", "?")), g.get("result")
        how = f" ({g['termination']})" if g.get("termination") else ""
        results.append(f"{w} beat {b}{how}" if res == "1-0" else f"{b} beat {w}{how}" if res == "0-1"
                       else f"{w} and {b} drew{how}")
    nxt = next((r for r in _rr_rounds(state) if r.get("round") == rnd["round"] + 1), None)
    tease = ""
    if nxt and nxt.get("pairings"):
        top = nxt["pairings"][0]
        tease = f" Next round, board 1: {spoken(top['white'])} against {spoken(top['black'])}."
    fmt = state.get("format") or {}
    rr = fmt.get("rr_rounds")
    left = f" {rr - rnd['round']} round robin rounds remain." if rr and rr > rnd["round"] else ""
    return {"key": f"recap-{rnd['round']}", "game": last_game.get("id"), "words": "40 to 65",
            "facts": f"Round {rnd['round']} is over. Results: " + "; ".join(results) + ". Standings now: "
                     + "; ".join(_record(r) for r in _table(state, 5)) + "." + left + tease
                     + " Wrap up the round with drama (who climbed, who fell, the surprise) and tease what comes next."}


def interest(game: dict, marks: dict, ranks: dict, done_ply: int, last_game: str | None, idle_seconds: float) -> float:
    """How much a board deserves the next line. Higher = more interesting right now."""
    score = 0.0
    for name in (game.get("white"), game.get("black")):
        rank = ranks.get(name)
        if rank:
            score += max(0, 4 - rank) * 1.5  # the top three are the story
    fresh = (game.get("moves") or [])[done_ply:]
    for move in fresh:
        san = move.get("san", "")
        score += MARK_SCORE.get(str(marks.get(str(move["ply"])) or ""), 0.0)
        score += 10.0 if "#" in san else 1.0 if "+" in san else 0.0
        score += 0.7 if "x" in san else 0.0
    score += min(len(fresh), 4) * 0.3
    clocks = game.get("clocks") or {}
    if game.get("status") == "live" and min(clocks.get("white", 10 ** 9), clocks.get("black", 10 ** 9)) < 60000:
        score += 3.0  # time trouble
    score += min(idle_seconds / 60.0, 3.0)  # rotate: boards nobody talked about lately climb
    if game.get("id") == last_game:
        score -= 2.0
    return score


def spoken(name: str) -> str:
    if name in SPOKEN:
        return SPOKEN[name]
    match = STOCKFISH_DEPTH_NAME.match(str(name or "").strip())
    if match:
        return f"Stockfish at depth {say_number(int(match.group(1)))}"
    return name


def is_stockfish(player: dict) -> bool:
    text = " ".join(str(player.get(k) or "") for k in ("name", "model")).lower()
    return "stockfish" in text


def stockfish_depths(state: dict) -> dict[str, int]:
    """Stockfish players and their current search depth (the runner raises it by one per AI win)."""
    out = {}
    for player in state.get("players") or []:
        if not isinstance(player, dict) or not player.get("name") or not is_stockfish(player):
            continue
        depth = player.get("depth")
        try:
            out[player["name"]] = int(depth)
        except (TypeError, ValueError):
            continue
    return out


def player_label(state: dict, name: str) -> str:
    """Spoken name, with Stockfish's depth when its name does not already say it."""
    label = spoken(name)
    depth = stockfish_depths(state).get(name)
    if depth is not None and "depth" not in label:
        label += f" (searching at depth {say_number(depth)})"
    return label


def player_note(state: dict, name: str) -> str:
    """A player's latest note to itself ('' when none). The runner may store a string or {text: ...}."""
    for player in state.get("players") or []:
        if isinstance(player, dict) and player.get("name") == name:
            note = player.get("note")
            if isinstance(note, dict):
                note = note.get("text") or note.get("note") or note.get("content") or ""
            note = " ".join(str(note or "").split())
            return re.sub(r"\s*[\u2013\u2014]\s*", ", ", note)[:240]
    return ""


def analyse_game(game: dict, positions: dict) -> dict:
    """Replay a game with python-chess: Stockfish scores per ply from White's side (None = unknown) and the material left.

    The viewer's sidecar scores positions from the side to move; this turns them into White's point of view.
    """
    out = {"cps": [], "material": None}
    if chess is None:
        return out
    board = chess.Board()
    for mv in game.get("moves") or []:
        try:
            board.push_uci(mv["uci"])
        except (KeyError, ValueError):
            break
        rec = (positions or {}).get(board.fen()) or {}
        cp = rec.get("cp")
        out["cps"].append(None if cp is None else (cp if board.turn == chess.WHITE else -cp))
    out["material"] = sum(PIECE_VALUE.get(p.symbol().lower(), 0) for p in board.piece_map().values())
    return out


def band(cp: int | None) -> str:
    """Words for a White-side score (never raw numbers in speech)."""
    if cp is None:
        return ""
    a = abs(cp)
    side = "White" if cp > 0 else "Black"
    if a < 60:
        return "roughly equal"
    if a < 150:
        return f"slightly better for {side}"
    if a < 400:
        return f"clearly better for {side}"
    return f"winning for {side}"


def san_line(moves: list[dict], upto: int = 12) -> str:
    parts = []
    for m in moves[:upto]:
        parts.append((f"{(m['ply'] + 1) // 2}. " if m["side"] == "white" else "") + str(m.get("san", "")))
    return " ".join(parts)


def _key(name: str = "OPENROUTER_API_KEY") -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                return str(winreg.QueryValueEx(handle, name)[0])
        except OSError:
            return None
    return None


def build_context(game: dict, annotations: dict, from_ply: int, switched: bool = False, leaders: str = "",
                  reason: str = "", extra: str = "") -> str:
    """The facts the commentator may use: players, the last few moves with marks and the movers' own comments."""
    moves = game.get("moves") or []
    lines = [f"Board {game.get('board', '?')}, round {game.get('round', '?')}."
             + (" The commentary just moved to this board." if switched else ""),
             f"White: {spoken(game.get('white', '?'))}. Black: {spoken(game.get('black', '?'))}.",
             f"Moves played so far: {len(moves)} half-moves."]
    if leaders:
        lines.append(f"Tournament standings now: {leaders}.")
    for move in moves[max(0, from_ply - 2):]:
        mover = spoken(game.get(move["side"], move["side"]))
        mark = (annotations or {}).get(str(move["ply"])) or ""
        own = (move.get("comment") or "").strip()
        lines.append(f"- {mover} ({move['side']}) played {move['san']}{mark}"
                     + (f" ({mark} from Stockfish)" if mark else "")
                     + (f". Its own reason: {own[:220]}" if own else ""))
    if game.get("status") != "live" and game.get("termination"):
        lines.append(f"The game is over: {game.get('result')} - {game['termination']}.")
    if reason == "opening":
        lines.append(f"Opening moves: {san_line(moves)}")
    if extra:
        lines.append(extra)
    if reason in REASON_HINT:
        lines.append(REASON_HINT[reason])
    return "\n".join(lines)


def clean_line(text: str) -> str:
    text = re.sub(r"\s*[\u2013\u2014]\s*", ", ", re.sub(r"[*_#`]", "", text or ""))
    text = " ".join(text.split())
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if len(text) <= MAX_LINE_CHARS:
        return text
    cut = text[:MAX_LINE_CHARS]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > MAX_LINE_CHARS // 3 else cut.rsplit(" ", 1)[0]   # whole sentences, never mid-word


# ---- route selection ------------------------------------------------------------------------------
def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_settings(route: str | None = None, model: str | None = None, effort: str | None = None,
                     tts: str | None = None, voice: str | None = None, allow_openrouter: bool | None = None,
                     calls_per_hour: int | None = None, env: dict | None = None) -> dict:
    """The text route, model, effort, voice and pacing (arguments beat COMMENTARY_* variables beat defaults).

    Raises ValueError for an unknown route, or a metered OpenRouter route without the explicit opt-in."""
    env = os.environ if env is None else env
    route = (route or env.get("COMMENTARY_ROUTE") or DEFAULT_ROUTE).strip().lower()
    if route not in ROUTES:
        raise ValueError(f"unknown commentary route {route!r}; expected one of {', '.join(ROUTES)}")
    allow = _truthy(env.get("COMMENTARY_ALLOW_OPENROUTER")) if allow_openrouter is None else bool(allow_openrouter)
    tts = (tts or env.get("COMMENTARY_TTS") or "edge").strip().lower()
    if tts not in TTS_ROUTES:
        raise ValueError(f"unknown commentary voice route {tts!r}; expected one of {', '.join(TTS_ROUTES)}")
    for what, value in (("text", route), ("voice", tts)):
        if value == "openrouter" and not allow:
            raise ValueError(f"the {what} route openrouter is metered; set COMMENTARY_ALLOW_OPENROUTER=1 to opt in")
    try:
        cap = int(calls_per_hour if calls_per_hour is not None else env.get("COMMENTARY_CALLS_PER_HOUR") or DEFAULT_CALLS_PER_HOUR)
    except ValueError:
        cap = DEFAULT_CALLS_PER_HOUR
    try:
        burst = int(env.get("COMMENTARY_BURST") or DEFAULT_BURST)
    except ValueError:
        burst = DEFAULT_BURST
    return {"route": route, "model": model or env.get("COMMENTARY_MODEL") or DEFAULT_MODELS[route],
            "effort": (effort or env.get("COMMENTARY_EFFORT") or DEFAULT_EFFORT).strip().lower(),
            "tts": tts, "voice": voice or env.get("COMMENTARY_VOICE") or EDGE_VOICE,
            "rate": env.get("COMMENTARY_VOICE_RATE") or EDGE_RATE, "allow_openrouter": allow,
            "calls_per_hour": max(1, cap), "burst": max(1, burst)}


def resolve_binary(name: str) -> str | None:
    """The CLI for a subscription route: override, native Windows exe, PATH, then the usual Linux homes."""
    override = os.environ.get(f"COMMENTARY_{name.upper()}_BIN") or os.environ.get(f"LLM_{name.upper()}_BIN")
    if override:
        return override
    if os.name == "nt" and WINDOWS_BINS[name].exists():
        return str(WINDOWS_BINS[name])   # the native exe avoids cmd.exe quoting of the system prompt
    found = shutil.which(name)
    if found:
        return found
    for candidate in LINUX_BINS.get(name, ()):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def isolated_workdir() -> Path:
    path = Path(tempfile.gettempdir()) / "llm-commentary-isolated"
    path.mkdir(parents=True, exist_ok=True)
    return path


def codex_command(binary: str, model: str, effort: str, last_message: Path) -> list[str]:
    argv = [binary, "exec", "--skip-git-repo-check", "--ignore-user-config", "--ephemeral", "--sandbox", "read-only",
            "-m", model, "-c", f"model_reasoning_effort={effort}", "-c", "project_doc_max_bytes=0",
            "-c", "web_search=disabled"]
    for feature in CODEX_DISABLED_FEATURES:
        argv += ["--disable", feature]
    return argv + ["--json", "--color", "never", "-o", str(last_message), "-"]


def claude_command(binary: str, model: str, effort: str, settings: Path) -> list[str]:
    return [binary, "-p", "--model", model, "--effort", effort, "--tools", "", "--strict-mcp-config",
            "--no-session-persistence", "--setting-sources", "", "--settings", str(settings),
            "--system-prompt", HOST_PROMPT, "--output-format", "json"]


def codex_tool_items(jsonl: str) -> set[str]:
    used: set[str] = set()
    for line in (jsonl or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        kind = str(item.get("type") or "") if isinstance(item, dict) else ""
        if kind in CODEX_TOOL_ITEMS:
            used.add(kind)
    return used


def provider_unavailable(error: str) -> bool:
    lowered = (error or "").lower()
    return any(marker in lowered for marker in UNAVAILABLE_MARKERS)


class RouteError(RuntimeError):
    pass


def run_cli(argv: list[str], prompt: str, timeout: float, workdir: Path, strip: tuple[str, ...]) -> str:
    env = os.environ.copy()
    for key in strip:
        env.pop(key, None)
    try:
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              cwd=str(workdir), env=env, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise RouteError(f"{Path(argv[0]).name} timed out after {timeout:.0f}s") from exc
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RouteError(f"{Path(argv[0]).name} exited {proc.returncode}: {output[-400:]!r}")
    return proc.stdout or ""


# ---- voice ----------------------------------------------------------------------------------------
def mp3_to_pcm(mp3: bytes, runner=subprocess.run) -> bytes:
    """Decode the edge-tts MP3 to raw 24 kHz mono signed 16-bit PCM (ffmpeg, else the miniaudio wheel)."""
    ffmpeg = os.environ.get("COMMENTARY_FFMPEG") or shutil.which("ffmpeg")
    if ffmpeg:
        proc = runner([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-acodec",
                       "pcm_s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
                      input=mp3, capture_output=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg exited {proc.returncode}: {(proc.stderr or b'')[-200:]!r}")
        return proc.stdout
    try:
        import miniaudio  # noqa: PLC0415 - optional pure wheel when ffmpeg is missing
    except ImportError as exc:
        raise RuntimeError("no ffmpeg and no miniaudio to decode the edge-tts MP3") from exc
    decoded = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=SAMPLE_RATE)
    return decoded.samples.tobytes()


def edge_tts_mp3(text: str, voice: str, rate: str = EDGE_RATE, timeout: float = EDGE_TIMEOUT_SECONDS) -> bytes:
    import edge_tts  # noqa: PLC0415 - optional: pip install edge-tts

    async def collect() -> bytes:
        audio = bytearray()
        async for chunk in edge_tts.Communicate(text, voice, rate=rate).stream():
            if chunk.get("type") == "audio":
                audio += chunk.get("data") or b""
        return bytes(audio)

    return asyncio.run(asyncio.wait_for(collect(), timeout))


def voice_ready() -> str:
    """'' when edge-tts can voice lines here, else why not."""
    try:
        import edge_tts  # noqa: F401, PLC0415
    except ImportError:
        return "edge-tts is not installed (pip install --user edge-tts)"
    if not (os.environ.get("COMMENTARY_FFMPEG") or shutil.which("ffmpeg")):
        try:
            import miniaudio  # noqa: F401, PLC0415
        except ImportError:
            return "no ffmpeg (or miniaudio) to decode the edge-tts MP3 to WAV"
    return ""


def write_wav(path: Path, pcm: bytes) -> float:
    pcm = pcm[: MAX_CLIP_SECONDS * SAMPLE_RATE * 2]
    if len(pcm) % 2:
        pcm = pcm[:-1]
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm)
    return len(pcm) / (2 * SAMPLE_RATE)


class Pacer:
    """At most `per_hour` calls in any rolling hour, spread by a token bucket of `burst` calls.

    Low-priority calls (thinking filler, routine updates) stop at 75% of the hourly cap."""

    LOW_SHARE = 0.75

    def __init__(self, per_hour: int, burst: int, clock=time.time) -> None:
        self.per_hour = max(1, int(per_hour))
        self.burst = max(1, int(burst))
        self.clock = clock
        self.calls: deque[float] = deque()
        self.tokens = float(self.burst)
        self.refilled = clock()
        self.total = 0

    def _prune(self, now: float) -> None:
        while self.calls and now - self.calls[0] >= 3600:
            self.calls.popleft()
        self.tokens = min(float(self.burst), self.tokens + (now - self.refilled) * self.per_hour / 3600.0)
        self.refilled = now

    def allowed(self, low: bool = False) -> bool:
        now = self.clock()
        self._prune(now)
        limit = self.per_hour * (self.LOW_SHARE if low else 1.0)
        return len(self.calls) < limit and self.tokens >= 1.0

    def take(self, low: bool = False) -> bool:
        if not self.allowed(low):
            return False
        self.calls.append(self.clock())
        self.tokens -= 1.0
        self.total += 1
        return True

    def last_hour(self) -> int:
        self._prune(self.clock())
        return len(self.calls)


class Commentator:
    def __init__(self, state_path: Path, out_dir: Path, log=print, budget_usd: float | None = None,
                 follow_dir: Path | None = None, **settings) -> None:
        self.state_path = Path(state_path)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.follow_dir = Path(follow_dir) if follow_dir else None
        self.config_error = ""
        try:
            self.settings = resolve_settings(**settings)
        except ValueError as exc:
            self.config_error = str(exc)
            self.settings = resolve_settings(route=DEFAULT_ROUTE, tts="edge")
        self.route, self.model, self.effort = self.settings["route"], self.settings["model"], self.settings["effort"]
        self.tts, self.voice = self.settings["tts"], self.settings["voice"]
        self.pacer = Pacer(self.settings["calls_per_hour"], self.settings["burst"])
        self.budget_usd = float(os.environ.get("COMMENTARY_BUDGET_USD",
                                             DEFAULT_BUDGET_USD if budget_usd is None else budget_usd))
        self.ledger = self.out_dir / "commentary-ledger.json"
        saved = self._load_ledger()
        self.spent_usd = float(saved.get("spent_usd", 0.0))
        self.calls_total = int(saved.get("calls_total", 0))
        self._seq = max(int(saved.get("seq", 0) or 0), self._global_seq())
        self._focus: str | None = None
        self._last_poll = 0.0
        self.always = os.environ.get("COMMENTARY_ALWAYS", "").strip() in {"1", "true", "yes"}
        self._busy_until = 0.0
        self._backoff_until = 0.0
        self._capped_logged = False
        self._last_text_s = 0.0
        self._prep_s: float | None = None
        self.gating = True        # False = every new move may get a line (old behaviour, tests)
        self._pos_cache: tuple[float, dict] = (0.0, {})
        # The viewer hands over its live (in memory) Stockfish marks and scores; the sidecar file lags behind them.
        self.marks_provider = None       # () -> {game: {ply: mark}}
        self.positions_provider = None   # () -> {fen: {"cp", "second", "best", "over"}}
        self.cost_async = True    # openrouter voice: look the TTS cost up after the clip is published (tests: False)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.http = self._http    # openrouter route only; replaceable in tests
        self.runner = run_cli     # subscription CLIs; replaceable in tests
        self.text_backend = None  # (system, user, max_tokens, temperature) -> str; replaces the route in tests
        self.tts_backend = None   # (text) -> pcm bytes; replaces the voice in tests
        self._reset_memory()
        self.tournament = saved.get("tournament")
        self._restore(saved)
        self._intro_due = False

    # ------------------------------------------------------------ per-tournament memory
    def _reset_memory(self) -> None:
        self._clips: dict[str, list[dict]] = {}
        self._done_ply: dict[str, int] = {}
        self._events_done: set = set()
        self._last_game: str | None = None
        self._last_said: dict[str, float] = {}
        self._quiet_from = 0.0    # when the last line finishes playing (about)
        self._reasons: dict[str, tuple[str, float]] = {}
        self._thought: set = set()  # (game, ply) whose live thinking was already read out
        self._said: dict[str, set] = {}      # game -> one-time reasons already spoken
        self._last_blunder: dict[str, int] = {}   # game -> ply of the last blunder line
        self._quiet_since: float | None = None   # nothing worth saying since (None = just spoke)
        self._tour_until = 0.0    # the viewer is on a time-lapse tour: stay silent until then
        self._tour_snapshot: dict | None = None
        self._tour_summary_due = False
        self._tours = 0
        self._sf_depth: dict[str, int] = {}
        self._notes_quoted: set = set()
        self._lines_since_note = NOTE_EVERY_LINES

    def _restore(self, saved: dict) -> None:
        self._clips = saved.get("clips", {}) or {}
        self._seq = max([self._seq] + [c["seq"] for clips in self._clips.values() for c in clips])
        self._done_ply = {g: max(c["ply"] for c in clips) for g, clips in self._clips.items() if clips}
        self._events_done = set(saved.get("events", []))
        self._sf_depth = {k: int(v) for k, v in (saved.get("sf_depth") or {}).items()}
        self._notes_quoted = set(saved.get("notes_quoted", []))

    def _global_seq(self) -> int:
        """Clip numbers never restart: the page and the relay key clips by seq (clip-N.wav is immutable)."""
        try:
            return int((self.out_dir.parent / "commentary-last-seq.txt").read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return 0

    def _sync_tournament(self) -> dict:
        """Read the state, following the newest tournament file in follow_dir; a new id resets the memory."""
        if self.follow_dir is not None:
            try:
                files = sorted(self.follow_dir.glob("*-tournament.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            except OSError:
                files = []
            if files and files[0].resolve() != self.state_path.resolve():
                self.log(f"commentary: following the newer tournament file {files[0].name}")
                old_slug = _slug(self.state_path)
                self.state_path = files[0]
                if self.out_dir.name == f"{old_slug}-commentary":
                    self.out_dir = self.state_path.parent / f"{_slug(self.state_path)}-commentary"
                    self.out_dir.mkdir(parents=True, exist_ok=True)
                    self.ledger = self.out_dir / "commentary-ledger.json"
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        key = str(state.get("id") or _slug(self.state_path))
        if self.tournament is None:
            self.tournament = key
        elif key != self.tournament:
            self._switch(key, state)
        return state

    def _switch(self, key: str, state: dict) -> None:
        with self._lock:
            previous = self.tournament
            self.tournament = key
            self._reset_memory()
            self._focus = None
            self._busy_until = 0.0
            saved = self._load_ledger()
            if saved.get("tournament") == key:
                self._restore(saved)   # a restart in the middle of this tournament
            self._intro_due = "opening" not in self._events_done
            self._sf_depth = self._sf_depth or stockfish_depths(state)
            self._save_ledger()
        self.log(f"commentary: new tournament {key} (was {previous}): memory reset, fresh intro due")

    # ------------------------------------------------------------ viewer API
    def start(self) -> bool:
        problem = self.readiness()
        if problem:
            self.log(f"commentary off: {problem}")
            return False
        threading.Thread(target=self._loop, daemon=True).start()
        voice = f"edge-tts {self.voice}" if self.tts == "edge" else ("OpenRouter " + TTS_MODEL if self.tts == "openrouter" else "no voice")
        spend = f", budget ${self.budget_usd:.2f}, spent ${self.spent_usd:.4f}" if self._metered() else ", spend 0 (subscription)"
        self.log(f"commentary: on ({self.route} {self.model} effort {self.effort} + {voice}, "
                 f"cap {self.pacer.per_hour} calls/hour{spend})")
        return True

    def readiness(self) -> str:
        """'' when the text route and the voice can run here, else why not."""
        if self.config_error:
            return self.config_error
        if self.route in ("codex", "claude") and not resolve_binary(self.route):
            return f"{self.route} CLI not found (install it or set COMMENTARY_{self.route.upper()}_BIN)"
        if self.route == "opencode-go" and not _key("OPENCODE_GO_API_KEY"):
            return "OPENCODE_GO_API_KEY is not set"
        if "openrouter" in (self.route, self.tts) and not _key():
            return "OPENROUTER_API_KEY is not set"
        if self.tts == "edge":
            return voice_ready()
        return ""

    def stop(self) -> None:
        self._stop.set()

    def focus(self, game_id: str | None, pinned: bool = False) -> None:
        """A pinned board (viewer focus mode) keeps the commentary; None or an unpinned hint = roam."""
        with self._lock:
            if not game_id:
                self._focus = None
            elif pinned:
                self._focus = game_id

    def clips(self, game_id: str, after_seq: int = 0) -> list[dict]:
        now = self._last_poll = time.time()
        with self._lock:
            return [dict(c, game=game_id, age_s=round(now - c.get("t", now), 1))
                    for c in self._clips.get(game_id, []) if c["seq"] > after_seq]

    def clips_all(self, after_seq: int = 0) -> list[dict]:
        now = self._last_poll = time.time()
        with self._lock:
            found = [dict(c, game=g, age_s=round(now - c.get("t", now), 1))
                     for g, clips in self._clips.items() for c in clips if c["seq"] > after_seq]
        return sorted(found, key=lambda c: c["seq"])

    def quiet_s(self) -> float:
        """How long the host has had nothing worth saying (0 while it is talking or held)."""
        since = self._quiet_since
        return 0.0 if since is None else round(max(0.0, time.time() - since), 1)

    def held(self) -> bool:
        return time.time() < self._tour_until

    def tour(self, on: bool) -> None:
        """The viewer starts (on) or ends (off) a time-lapse tour of all boards: silence, then a summary."""
        with self._lock:
            if on:
                self._tour_until = time.time() + TOUR_HOLD_MAX
                self._quiet_since = None
                try:
                    state = json.loads(self.state_path.read_text(encoding="utf-8"))
                    self._tour_snapshot = self._snapshot(state)
                except (OSError, ValueError):
                    self._tour_snapshot = None
            else:
                was = self._tour_until > 0
                self._tour_until = 0.0
                self._tour_summary_due = was and self._tour_snapshot is not None

    def _positions(self, state: dict) -> dict:
        if self.positions_provider is not None:
            try:
                return self.positions_provider() or {}
            except Exception:
                pass
        sidecar = self.state_path.with_name(f"{state.get('id', '')}-annotations.json")
        try:
            mtime = sidecar.stat().st_mtime
            if mtime != self._pos_cache[0]:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                self._pos_cache = (mtime, data.get("positions") or {} if isinstance(data, dict) else {})
        except (OSError, ValueError):
            pass
        return self._pos_cache[1]

    def _snapshot(self, state: dict) -> dict:
        pos = self._positions(state)
        snap = {}
        for gid, g in (state.get("games") or {}).items():
            if g.get("status") != "live":
                continue
            cps = [c for c in analyse_game(g, pos)["cps"] if c is not None]
            snap[gid] = {"plies": len(g.get("moves") or []), "cp": cps[-1] if cps else None}
        return snap

    def _tour_event(self, state: dict) -> dict | None:
        """What changed on the boards during the time lapse (facts for one spoken summary)."""
        snap = self._tour_snapshot or {}
        games = state.get("games") or {}
        pos = self._positions(state)
        lines, best, best_swing = [], None, -1.0
        for gid in sorted(snap, key=lambda k: games.get(k, {}).get("board", 99)):
            g = games.get(gid) or {}
            w, b = spoken(g.get("white", "?")), spoken(g.get("black", "?"))
            before = snap[gid]
            if g.get("status") != "live" and g.get("result", "*") != "*":
                res = g.get("result")
                how = f" ({g['termination']})" if g.get("termination") else ""
                lines.append(f"Board {g.get('board')}: {w} beat {b}{how}" if res == "1-0" else
                             f"Board {g.get('board')}: {b} beat {w}{how}" if res == "0-1" else
                             f"Board {g.get('board')}: {w} and {b} drew{how}")
                best = best or gid
                continue
            cps = [c for c in analyse_game(g, pos)["cps"] if c is not None]
            now = cps[-1] if cps else None
            moved = len(g.get("moves") or []) - before["plies"]
            if now is None or before["cp"] is None:
                lines.append(f"Board {g.get('board')}: {w} against {b}, {moved} more half-moves")
            else:
                swing = abs(now - before["cp"])
                changed = band(now) != band(before["cp"])
                lines.append(f"Board {g.get('board')}: {w} against {b}, now {band(now)}"
                             + (f" (was {band(before['cp'])})" if changed else ""))
                if swing > best_swing:
                    best, best_swing = gid, swing
        if not lines:
            return None
        best = best or next((g for g in snap if g in games), None)
        if best is None:
            return None
        self._tours += 1
        return {"key": f"tour-{self._tours}", "game": best, "words": "30 to 50",
                "facts": "A time-lapse tour of every board just ended. What the boards look like now: " + "; ".join(lines)
                         + ". Sum up in a few lively sentences what changed while time flew by and which board to watch."}

    def _depth_event(self, state: dict) -> dict | None:
        """Stockfish's depth went up (an AI beat it): announce the new depth once."""
        depths = stockfish_depths(state)
        games = state.get("games") or {}
        for name, depth in depths.items():
            old = self._sf_depth.get(name)
            if old is None or depth < old:
                self._sf_depth[name] = depth   # first sight (start, resume, new tournament): no announcement
                continue
            key = f"sfdepth-{depth}"
            if depth == old or key in self._events_done:
                self._sf_depth[name] = depth   # announced (or nothing new): this is the new baseline
                continue
            lost = [g for g in games.values() if g.get("result") in ("1-0", "0-1")
                    and ((g.get("white") == name and g.get("result") == "0-1")
                         or (g.get("black") == name and g.get("result") == "1-0"))]
            lost.sort(key=lambda g: _end_epoch(g) or 0)
            game = lost[-1] if lost else next((g for g in games.values() if name in (g.get("white"), g.get("black"))
                                               and g.get("status") == "live"), None)
            if game is None:
                game = next(iter(games.values()), None)
            if game is None:
                continue
            winner = ""
            if lost:
                winner = game.get("black") if game.get("white") == name else game.get("white")
            facts = ((f"{spoken(winner)} just beat Stockfish. " if winner else "An AI just beat Stockfish. ")
                     + f"So Stockfish's search depth goes up from {say_number(old)} to {say_number(depth)}. "
                     "Every win makes it stronger. Who can beat it at this depth?")
            return {"key": key, "game": game["id"], "words": "20 to 35", "facts": facts}
        return None

    def audio_path(self, name: str) -> Path | None:
        if not re.fullmatch(r"clip-\d+\.wav", name or ""):
            return None
        path = self.out_dir / name
        return path if path.exists() else None

    # ------------------------------------------------------------ worker
    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.tick()
            except Exception as exc:  # never take the viewer down
                self.log(f"commentary: {type(exc).__name__}: {str(exc)[:300]}")
                time.sleep(5)

    def _metered(self) -> bool:
        return self.route in METERED_ROUTES or self.tts == "openrouter"

    def tick(self) -> dict | None:
        now = time.time()
        if self._metered() and self.spent_usd >= self.budget_usd:
            return None
        if now < self._busy_until or now < self._backoff_until:
            return None
        if not self.always and now - self._last_poll > LISTENER_SECONDS:
            return None  # nobody is listening
        state = self._sync_tournament()
        games = state.get("games") or {}
        moving = time.time() - (state.get("updated_epoch_ms") or 0) / 1000 < LIVE_STATE_SECONDS
        if self.held():
            return None  # a time-lapse tour is running
        if self._intro_due:
            if "opening" in self._events_done:
                self._intro_due = False
            elif moving:
                intro = opening_event(state, fresh=True)
                if intro and intro.get("game") in games:
                    clip = self._emit_event(intro, games[intro["game"]])
                    self._intro_due = "opening" not in self._events_done
                    return clip
        if self._tour_summary_due and moving:
            self._tour_summary_due = False
            tour_event = self._tour_event(state)
            if tour_event:
                return self._emit_event(tour_event, games[tour_event["game"]])
        event = next_event(state, self._events_done) or self._depth_event(state)
        if event and event["key"] in {"opening"} | {f"round-{state.get('current_round')}"} and not moving:
            event = None  # a paused round looks "live" with no moves: preview it when it really starts
        if event and event.get("game") in games:
            return self._emit_event(event, games[event["game"]])
        all_marks = self._annotations(state)
        game_id = self.pick(state, all_marks)
        if game_id is None:
            if moving and self._quiet_since is None:
                self._quiet_since = time.time()
            return self._think_line(state, all_marks) if (moving and THINK_LINES) else None
        game = games[game_id]
        reason = self._reasons.get(game_id, ("", 0.0))[0] if self.gating else ""
        plies = len(game.get("moves") or [])
        done = self._done_ply.get(game_id, 0)
        finished = game.get("status") != "live" and game.get("result", "*") != "*"
        if reason in LOW_PRIORITY_REASONS and not self.pacer.allowed(low=True):
            return None   # near the hourly cap: routine updates wait, big moments keep the room
        annotations = all_marks.get(game_id, {})
        top = [r for r in (state.get("standings") or []) if r.get("played")][:3]
        leaders = "; ".join(f"{spoken(r['name'])} {r['points']:g} point{'' if r['points'] == 1 else 's'}" for r in top)
        extra = []
        if reason in {"endgame", "decided", "draw"}:
            cps = [c for c in analyse_game(game, self._positions(state))["cps"] if c is not None]
            if cps:
                extra.append(f"Stockfish's verdict now: {band(cps[-1])}.")
        extra += self._player_facts(state, game)
        note, note_key = self._note_offer(state, game)
        if note:
            extra.append(note)
        context = build_context(game, annotations, 1 if reason == "opening" else done + 1,
                                switched=game_id != self._last_game, leaders=leaders, reason=reason,
                                extra="\n".join(extra))
        text = self._chat(COMMENTATOR_PROMPT, context, 120, 0.8)
        if text is None:
            return None   # capped or backing off: the moment stays open for the next tick
        self._done_ply[game_id] = plies
        if reason in {"opening", "endgame", "decided", "draw", "time"}:
            self._said.setdefault(game_id, set()).add(reason)
        if reason in {"blunder", "mistake"}:
            self._last_blunder[game_id] = plies
        if not text:
            return None
        self._lines_since_note += 1
        if note_key:   # offered once: the host quotes it now or lets it go
            self._notes_quoted.add("|".join(note_key))
            self._lines_since_note = 0
        clip = self._publish(game_id, text, plies, {"final": finished, **({"reason": reason} if reason else {})})
        self.log(f"commentary {game_id} ply {plies}: {text}{self._tally()}")
        return clip

    def _tally(self) -> str:
        if self._metered():
            return f" (total ${self.spent_usd:.4f})"
        return f" ({self.pacer.last_hour()} calls in the last hour, {self.calls_total} total)"

    def _player_facts(self, state: dict, game: dict) -> list[str]:
        out = []
        depths = stockfish_depths(state)
        for side in ("white", "black"):
            name = game.get(side)
            if name in depths:
                out.append(f"{spoken(name)} plays {side} here, searching at depth {say_number(depths[name])} "
                           "(its depth rises by one each time an AI beats it).")
        return out

    def _note_offer(self, state: dict, game: dict) -> tuple[str, tuple[str, str] | None]:
        """Now and then, a player's latest note to itself the host may quote (each note at most once)."""
        if self._lines_since_note < NOTE_EVERY_LINES:
            return "", None
        for side in ("white", "black"):
            name = game.get(side) or ""
            note = player_note(state, name)
            if len(note) < 12:
                continue
            key = (name, note[:80])
            if "|".join(key) in self._notes_quoted:
                continue
            return (f"{spoken(name)} keeps notes between games. Its latest note to itself: \"{note}\". "
                    "You may quote a few words of it if it fits this moment."), key
        return "", None

    def _publish(self, game_id: str, text: str, ply: int, extra: dict) -> dict | None:
        """Voice a line and hand it to the viewer at once; a failed voice keeps the host silent for this line."""
        started = time.monotonic()
        voiced = self._speak(text)
        # Writing plus voicing time (EMA): the next line starts this long before the current one ends.
        # Measured 2026-10-08 on the VPS: codex luna 3.6-4.5 s, edge-tts 0.5-0.85x the clip length.
        prep = self._last_text_s + (time.monotonic() - started)
        self._prep_s = prep if self._prep_s is None else 0.7 * self._prep_s + 0.3 * prep
        if voiced is None:
            return None
        pcm, generation_id = voiced
        with self._lock:
            self._seq += 1
            name = f"clip-{self._seq}.wav"
            seconds = write_wav(self.out_dir / name, pcm)
            now = time.time()
            clip = {"seq": self._seq, "ply": ply, "text": text, "audio": name, "seconds": round(seconds, 1),
                    "t": round(now, 2), **extra}
            self._clips.setdefault(game_id, []).append(clip)
            self._last_game = game_id
            self._last_said[game_id] = now
            # The next line is written while this one plays, so it is ready as this one ends.
            lead = LEAD_SECONDS if self.route in METERED_ROUTES else CLI_LEAD_SECONDS
            lead = max(lead, min(30.0, (self._prep_s or 0.0) + 1.0))
            self._quiet_since = None
            self._busy_until = now + max(MIN_GAP_SECONDS, seconds - lead)
            self._quiet_from = now + seconds + 1.5
            self._save_ledger()
        if self.tts == "openrouter":
            if self.cost_async:
                threading.Thread(target=self._book_cost, args=(generation_id,), daemon=True).start()
            else:
                self._book_cost(generation_id)
        return clip

    def _book_cost(self, generation_id: str | None) -> None:
        cost = self._generation_cost(generation_id)
        with self._lock:
            self.spent_usd += cost
            self._save_ledger()

    def _emit_event(self, event: dict, game: dict) -> dict | None:
        text = self._chat(EVENT_PROMPT.format(words=event.get("words", "25 to 45")), event["facts"], 260, 0.9)
        if text is None:
            return None
        self._events_done.add(event["key"])
        if not text:
            return None
        clip = self._publish(game["id"], text, len(game.get("moves") or []),
                             {"final": event["key"] == "champion", "event": event["key"]})
        self.log(f"commentary event {event['key']}: {text}{self._tally()}")
        return clip

    def _think_line(self, state: dict, all_marks: dict) -> dict | None:
        """No new move anywhere and the line before has finished: read what a model is weighing right now."""
        if time.time() - self._quiet_from < FILLER_GAP_SECONDS:
            return None
        if not self.pacer.allowed(low=True):
            return None   # filler is the first thing to go near the hourly cap
        games = state.get("games") or {}
        with self._lock:
            pinned = self._focus if self._focus in games else None
        ranks = {r["name"]: r.get("rank") for r in state.get("standings") or [] if r.get("played")}
        now = time.time()
        best, best_score, best_text = None, float("-inf"), ""
        for game_id, game in games.items():
            if game.get("status") != "live" or (pinned and game_id != pinned):
                continue
            plies = len(game.get("moves") or [])
            if (game_id, plies + 1) in self._thought:
                continue
            text = self._thinking_tail(state, game_id, plies + 1)
            if len(text) < 300:
                continue
            idle = now - self._last_said.get(game_id, now - 180)
            score = interest(game, all_marks.get(game_id, {}), ranks, plies, self._last_game, idle)
            if score > best_score:
                best, best_score, best_text = game_id, score, text
        if best is None:
            return None
        game = games[best]
        plies = len(game.get("moves") or [])
        self._thought.add((best, plies + 1))
        side = "white" if plies % 2 == 0 else "black"
        other = "black" if side == "white" else "white"
        last = (game.get("moves") or [{}])[-1] if plies else {}
        context = (f"Board {game.get('board', '?')}, round {game.get('round', '?')}. "
                   f"{spoken(game.get(side, side))} ({side}) is thinking about its move right now against "
                   f"{spoken(game.get(other, other))}."
                   + (f" The last move was {spoken(game.get(last.get('side'), ''))} playing {last.get('san')}." if last else "")
                   + (" The commentary just moved to this board." if best != self._last_game else "")
                   + f"\nIts live thinking so far (latest part):\n{best_text}")
        text = self._chat(THINKING_PROMPT, context, 120, 0.8, low=True)
        if not text:
            return None
        clip = self._publish(best, text, plies, {"final": False, "thinking": True})
        self.log(f"commentary {best} thinking ply {plies + 1}: {text}{self._tally()}")
        return clip

    def _thinking_tail(self, state: dict, game_id: str, ply: int, limit: int = 1500) -> str:
        path = self.state_path.parent / f"{state.get('id', '')}-{game_id}-ply{ply}.thinking.txt"
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - limit * 3))
                text = handle.read().decode("utf-8", errors="ignore")
        except OSError:
            return ""
        return " ".join(text.split())[-limit:]

    def pick(self, state: dict, all_marks: dict) -> str | None:
        """The board for the next line: the pinned one, else the most interesting board with something new."""
        games = state.get("games") or {}
        with self._lock:
            pinned = self._focus if self._focus in games else None
        ranks = {r["name"]: r.get("rank") for r in state.get("standings") or [] if r.get("played")}
        now = time.time()
        positions = self._positions(state) if self.gating else {}
        self._reasons = {}
        best, best_score = None, float("-inf")
        for game_id, game in games.items():
            if pinned and game_id != pinned:
                continue
            plies = len(game.get("moves") or [])
            done = self._done_ply.get(game_id, 0)
            finished = game.get("status") != "live" and game.get("result", "*") != "*"
            recent_end = finished and self._ended_recently(game, now)
            result_due = recent_end and not self._said_result(game_id)
            if not (game.get("status") == "live" and plies > done) and not result_due:
                continue
            marks = all_marks.get(game_id, {})
            idle = now - self._last_said.get(game_id, now - 180)
            score = interest(game, marks, ranks, done, self._last_game, idle)
            if self.gating and not result_due:
                reason, rscore = self._board_reason(game_id, game, marks, positions, ranks)
                if not reason:
                    continue  # nothing worth saying on this board: it stays quiet
                self._reasons[game_id] = (reason, rscore)
                score += rscore
            if result_due:
                score += 8.0 if game.get("result") in ("1-0", "0-1") else 5.0
            if score > best_score:
                best, best_score = game_id, score
        return best

    def _board_reason(self, game_id: str, game: dict, marks: dict, positions: dict, ranks: dict) -> tuple[str, float]:
        """The strongest reason this live board deserves a line now ('' = boring: skip it)."""
        moves = game.get("moves") or []
        plies, done = len(moves), self._done_ply.get(game_id, 0)
        fresh = moves[done:]
        said = self._said.setdefault(game_id, set())
        found: dict[str, float] = {}
        if any("#" in str(m.get("san", "")) for m in fresh):
            found["mate"] = REASON_SCORE["mate"]
        bad = [str(marks.get(str(m["ply"])) or "") for m in fresh]
        if plies - self._last_blunder.get(game_id, -99) >= BLUNDER_GAP_PLIES:
            if "??" in bad:
                found["blunder"] = REASON_SCORE["blunder"]
            elif "?" in bad:
                found["mistake"] = REASON_SCORE["mistake"]
        clocks = game.get("clocks") or {}
        if "time" not in said and min(clocks.get("white", 10 ** 9), clocks.get("black", 10 ** 9)) < 45000:
            found["time"] = REASON_SCORE["time"]
        info = analyse_game(game, positions)
        cps = [c for c in info["cps"] if c is not None]
        decided = "decided" in said
        if "opening" not in said and plies >= OPENING_PLIES:
            found["opening"] = REASON_SCORE["opening"]
        if not decided:
            if "endgame" not in said and info["material"] is not None and info["material"] <= ENDGAME_MATERIAL \
                    and plies >= ENDGAME_MIN_PLIES:
                found["endgame"] = REASON_SCORE["endgame"]
            if len(cps) >= DECIDED_STREAK and all(abs(c) >= DECIDED_CP for c in cps[-DECIDED_STREAK:]) and plies >= 20:
                found["decided"] = REASON_SCORE["decided"]
            if "draw" not in said and plies >= DRAW_MIN_PLIES and len(cps) >= DRAW_STREAK \
                    and all(abs(c) <= DRAW_CP for c in cps[-DRAW_STREAK:]):
                found["draw"] = REASON_SCORE["draw"]
            top2 = any((ranks.get(n) or 99) <= 2 for n in (game.get("white"), game.get("black")))
            if "draw" not in said and top2 and plies - done >= LEADER_GAP_PLIES:
                found["leaders"] = REASON_SCORE["leaders"]
            if "draw" not in said and plies - done >= UPDATE_GAP_PLIES:
                found["update"] = REASON_SCORE["update"]
        if decided:   # a decided game: only the end (mate, a flag) is worth a line
            found = {k: v for k, v in found.items() if k in {"mate", "time"}}
        if not found:
            return "", 0.0
        reason = max(found, key=found.get)
        return reason, found[reason]

    @staticmethod
    def _ended_recently(game: dict, now: float) -> bool:
        try:
            import datetime as dt

            ended = dt.datetime.fromisoformat(str(game.get("end"))).timestamp()
        except (TypeError, ValueError):
            return True  # no end stamp: still worth one closing line
        return now - ended < RECENT_RESULT_SECONDS

    def _said_result(self, game_id: str) -> bool:
        return any(c.get("final") for c in self._clips.get(game_id, []))

    def _annotations(self, state: dict) -> dict:
        if self.marks_provider is not None:
            try:
                return self.marks_provider() or {}
            except Exception:
                pass
        sidecar = self.state_path.with_name(f"{state.get('id', '')}-annotations.json")
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            marks = data.get("annotations") if isinstance(data, dict) else None
            return marks if isinstance(marks, dict) else {}
        except (OSError, ValueError):
            return {}

    # ------------------------------------------------------------ text
    def _chat(self, mode: str, facts: str, max_tokens: int, temperature: float, low: bool = False) -> str | None:
        """One spoken passage from the configured route. None = no call made (hourly cap or backing off)."""
        if time.time() < self._backoff_until:
            return None
        if not self.pacer.take(low=low):
            if not self._capped_logged:
                self.log(f"commentary: hourly cap of {self.pacer.per_hour} calls reached; waiting")
                self._capped_logged = True
            return None
        self._capped_logged = False
        self.calls_total += 1
        asked = time.monotonic()
        user = mode + "\n\n" + facts
        try:
            if self.text_backend is not None:
                raw = self.text_backend(HOST_PROMPT, user, max_tokens, temperature)
            elif self.route == "codex":
                raw = self._ask_codex(user)
            elif self.route == "claude":
                raw = self._ask_claude(user)
            elif self.route == "opencode-go":
                raw = self._ask_opencode(user, max_tokens, temperature)
            else:
                raw = self._ask_openrouter(user, max_tokens, temperature)
        except Exception as exc:
            wait = UNAVAILABLE_BACKOFF_SECONDS if provider_unavailable(str(exc)) else ERROR_BACKOFF_SECONDS
            self._backoff_until = time.time() + wait
            self.log(f"commentary: {self.route} {self.model} failed ({type(exc).__name__}: {str(exc)[:300]}); "
                     f"pausing {wait}s")
            return None
        self._last_text_s = time.monotonic() - asked
        return clean_line(raw)

    def _ask_codex(self, user: str) -> str:
        binary = resolve_binary("codex")
        if not binary:
            raise RouteError("codex CLI not found")
        workdir = isolated_workdir()
        last = workdir / f"codex-last-{os.getpid()}-{time.time_ns()}.txt"
        try:
            output = self.runner(codex_command(binary, self.model, self.effort, last),
                                 HOST_PROMPT + "\n\n" + user, CLI_TIMEOUT_SECONDS, workdir, STRIPPED_ENV["codex"])
            used = codex_tool_items(output)
            if used:
                raise RouteError(f"codex used a tool ({', '.join(sorted(used))})")
            if not last.exists():
                raise RouteError(f"codex wrote no final message: {output[-300:]!r}")
            return last.read_text(encoding="utf-8", errors="replace")
        finally:
            last.unlink(missing_ok=True)

    def _ask_claude(self, user: str) -> str:
        binary = resolve_binary("claude")
        if not binary:
            raise RouteError("claude CLI not found")
        workdir = isolated_workdir()
        settings = workdir / "claude-settings.json"
        if not settings.exists():
            settings.write_text(json.dumps({"disableAllHooks": True}), encoding="utf-8")
        output = self.runner(claude_command(binary, self.model, self.effort, settings), user, CLI_TIMEOUT_SECONDS,
                             workdir, STRIPPED_ENV["claude"])
        start = output.find("{")
        try:
            data = json.loads(output[start:]) if start >= 0 else {}
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict) or data.get("is_error"):
            raise RouteError(f"claude error: {str(data.get('result') if isinstance(data, dict) else output)[:300]}")
        return str(data.get("result") or "")

    def _ask_opencode(self, user: str, max_tokens: int, temperature: float) -> str:
        key = _key("OPENCODE_GO_API_KEY")
        if not key:
            raise RouteError("OPENCODE_GO_API_KEY is not set")
        body = {"model": self.model, "max_tokens": max(1200, max_tokens), "temperature": temperature,
                "reasoning_effort": self.effort,
                "messages": [{"role": "system", "content": HOST_PROMPT}, {"role": "user", "content": user}]}
        request = urllib.request.Request(OPENCODE_GO_URL, data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                                  "User-Agent": BROWSER_UA})
        try:
            with urllib.request.urlopen(request, timeout=CLI_TIMEOUT_SECONDS) as response:
                reply = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise RouteError(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:200]}") from exc
        return ((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    def _ask_openrouter(self, user: str, max_tokens: int, temperature: float) -> str:
        body = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature,
                "messages": [{"role": "system", "content": HOST_PROMPT}, {"role": "user", "content": user}]}
        data, _headers = self.http("/chat/completions", body)
        reply = json.loads(data)
        self.spent_usd += float((reply.get("usage") or {}).get("cost") or 0.0)
        return ((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    # ------------------------------------------------------------ voice
    def _speak(self, text: str) -> tuple[bytes, str | None] | None:
        """PCM (24 kHz mono 16-bit) for the line, or None (logged) when the voice failed: no paid fallback."""
        try:
            if self.tts_backend is not None:
                pcm, generation_id = self.tts_backend(text), None
            elif self.tts == "edge":
                pcm, generation_id = mp3_to_pcm(edge_tts_mp3(text, self.voice, self.settings["rate"])), None
            elif self.tts == "openrouter":
                # Only the line itself is spoken; the delivery style goes in `instructions` (2026-10-06).
                body = {"model": TTS_MODEL, "input": text, "instructions": TTS_STYLE, "voice": TTS_VOICE,
                        "response_format": "pcm"}
                pcm, headers = self.http("/audio/speech", body)
                generation_id = headers.get("X-Generation-Id") or headers.get("x-generation-id")
            else:
                return None
        except Exception as exc:
            self.log(f"commentary: voice failed, line stays silent ({type(exc).__name__}: {str(exc)[:200]}): {text[:80]}")
            return None
        if not pcm:
            self.log(f"commentary: voice returned no audio, line stays silent: {text[:80]}")
            return None
        return pcm, generation_id

    def _generation_cost(self, generation_id: str | None) -> float:
        if not generation_id:
            return 0.004  # conservative per-line estimate when the record is missing
        for _ in range(3):
            time.sleep(1.5)
            try:
                data, _ = self.http(f"/generation?id={generation_id}", None)
                return float(json.loads(data)["data"].get("total_cost") or 0.0)
            except Exception:
                continue
        return 0.004

    def _http(self, path: str, body: dict | None) -> tuple[bytes, dict]:
        headers = {"Authorization": f"Bearer {_key()}", "X-Title": "ai-chess-harness commentary"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(API + path, data=data, headers=headers, method="POST" if body is not None else "GET")
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read(), dict(response.headers.items())

    def _load_ledger(self) -> dict:
        try:
            return json.loads(self.ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_ledger(self) -> None:
        self.ledger.write_text(json.dumps({
            "tournament": self.tournament, "route": self.route, "model": self.model, "voice": self.voice,
            "spent_usd": round(self.spent_usd, 6), "calls_total": self.calls_total, "seq": self._seq,
            "sf_depth": self._sf_depth, "notes_quoted": sorted(self._notes_quoted),
            "clips": self._clips, "events": sorted(self._events_done)}, indent=1), encoding="utf-8")
        try:
            (self.out_dir.parent / "commentary-last-seq.txt").write_text(str(self._seq), encoding="utf-8")
        except OSError:
            pass


def _slug(state_path: Path) -> str:
    name = Path(state_path).name
    return name[: -len("-tournament.json")] if name.endswith("-tournament.json") else Path(state_path).stem


def main(argv: list[str] | None = None) -> int:
    """Smoke test: write N real lines for a tournament state through the configured route and voice them."""
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lines", type=int, default=3)
    args = parser.parse_args(argv)
    c = Commentator(args.state, args.out, log=lambda m: print(m, flush=True))
    problem = c.readiness()
    if problem:
        print(f"not ready: {problem}")
        return 2
    state = json.loads(args.state.read_text(encoding="utf-8"))
    games = sorted((state.get("games") or {}).values(), key=lambda g: (g.get("round", 0), g.get("board", 0)))
    finals = [g for g in games if g.get("moves")]
    jobs = [("event", opening_event({**state, "games": {g["id"]: dict(g, status="live") for g in finals[:1]}}))]
    recap_round = max((r.get("round", 0) for r in _rr_rounds(state)), default=0)
    if recap_round:
        rnd = next(r for r in _rr_rounds(state) if r.get("round") == recap_round)
        rgames = [state["games"].get(p.get("game_id")) or {} for p in rnd.get("pairings") or []]
        results = "; ".join(f"{spoken(g.get('white', '?'))} against {spoken(g.get('black', '?'))}: {g.get('result')}"
                            for g in rgames)
        jobs.append(("event", {"key": "recap", "game": rgames[0].get("id"), "words": "40 to 60",
                               "facts": f"Round {recap_round} is over. Results: {results}. Standings now: "
                                        + "; ".join(_record(r) for r in _table(state, 5)) + "."}))
    for g in finals:
        if len(jobs) >= args.lines:
            break
        jobs.append(("line", build_context(g, {}, max(1, len(g["moves"]) - 2), switched=True,
                                           extra="\n".join(c._player_facts(state, g)))))
    report = []
    for kind, job in jobs[: args.lines]:
        if job is None:
            continue
        started = time.monotonic()
        if kind == "event":
            text = c._chat(EVENT_PROMPT.format(words=job.get("words", "25 to 45")), job["facts"], 260, 0.9)
        else:
            text = c._chat(COMMENTATOR_PROMPT, job, 120, 0.8)
        text_s = time.monotonic() - started
        if not text:
            print(f"{kind}: no text ({text_s:.1f}s)")
            continue
        voiced_at = time.monotonic()
        clip = c._publish("smoke", text, 0, {"final": False})
        voice_s = time.monotonic() - voiced_at
        report.append({"kind": kind, "text": text, "text_s": round(text_s, 2), "voice_s": round(voice_s, 2),
                       "audio": clip and clip["audio"], "seconds": clip and clip["seconds"]})
        print(json.dumps(report[-1]), flush=True)
    return 0 if report else 1


if __name__ == "__main__":
    raise SystemExit(main())
