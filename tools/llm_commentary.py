"""Spoken commentary for the live LLM tournament viewer (OpenRouter, metered).

A background thread watches the tournament state JSON. It roams the boards like a TV commentator:
each line goes to the board that matters most right now (tournament leaders playing, fresh blunders
or strong moves, checks, captures, time trouble, a result just in), with a nudge to rotate boards.
A board the viewer pins (focus mode) keeps the commentary. At most one line every MIN_GAP_SECONDS,
written from the recent moves, the players' own move comments and the Stockfish move marks, then
voiced with an OpenRouter speech model. Clips are WAV files in out_dir; the viewer polls `clips()` and plays
them one at a time. A hard spending cap stops all calls once reached.

Every round opens with a spoken preview (storylines, the match of the round) and closes with a recap that teases
the next round. When no board has a new move, a short line reads what a model is weighing in its live thinking,
so the show never sits on music alone (owner 2026-10-06: "very good introductory hooks, and not just background
music with games"). Lines are timed to start as the previous one ends and are published before their cost is
looked up, so speech stays on the move it describes ("speech is sometimes delayed").

Measured 2026-10-06: Gemini 3.8 Flash TTS, a 12.5 s line = $0.0028 (PCM only, 24 kHz mono).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

API = "https://openrouter.ai/api/v1"
TEXT_MODEL = "google/gemini-3.1-flash-lite"
TTS_MODEL = "google/gemini-3.8-flash-tts"
TTS_VOICE = "Puck"
TTS_STYLE = "Speak like an excited but clear chess commentator."
SAMPLE_RATE = 24000
MIN_GAP_SECONDS = 6.0
LEAD_SECONDS = 5.0        # start writing the next line this long before the current one ends (voicing takes ~5 s)
FILLER_GAP_SECONDS = 6.0  # quiet this long after a line and no new move: read a model's live thinking
RECAP_SECONDS = 600       # a round that ended longer ago than this gets no recap (resume after a pause)
INTRO_MAX_PLIES = 6       # a round further along than this gets no preview
LIVE_STATE_SECONDS = 240  # state older than this = paused or stopped: no round preview, no thinking lines
# Only spend while someone listens: an unmuted page polls clips every 2 s (COMMENTARY_ALWAYS=1 overrides).
LISTENER_SECONDS = 20.0
DEFAULT_BUDGET_USD = 1.5
# Spoken names: say the version numbers the way a commentator would.
SPOKEN = {"GPT-6.1 Sol": "GPT six point one Sol", "Grok 4.7": "Grok four point seven",
          "Sonnet 5.5": "Sonnet five point five", "Opus 5.5": "Opus five point five",
          "DeepSeek V4.1 Flash": "DeepSeek Vee four point one Flash", "GLM 5.3 Flash": "GLM five point three Flash",
          "Stockfish 19 (depth 4)": "Stockfish at depth four", "Gemini 3.8 Flash": "Gemini three point eight Flash",
          "Qwen 3.8 Omni Flash": "Kwen three point eight Omni Flash", "MiMo V2.6 Pro": "Mimo Vee two point six Pro",
          "Muse Spark 1.3": "Muse Spark one point three"}
COMMENTATOR_PROMPT = (
    "You are a lively, sharp chess commentator for a YouTube tournament between AI models. "
    "Write ONE spoken line of 15 to 35 words about the latest moves on this board: name who moved, what it means, "
    "and react to any move mark (?? blunder, ? mistake, ?! inaccuracy, ! strong move). You may quote the player's own "
    "reason in a few words. Plain words only: no markdown, no lists, no dashes, no emojis, no move numbers, "
    "write moves the way they are spoken (Knight takes e5, castles short). Do not invent moves or evaluations. "
    "When the notes say the commentary just moved to this board, start by naming the board (for example "
    "'Over on board three'). Mention the tournament standings only when the notes give them and it adds drama."
)
EVENT_PROMPT = (
    "You are the hype host of a YouTube chess tournament between AI models. Write ONE spoken passage of {words} words "
    "for the moment described. Big energy, vivid, specific to the names and facts given, a hook that makes viewers stay. "
    "Plain words only: no markdown, no lists, no dashes, no emojis, no move numbers. Do not invent results or facts."
)
THINKING_PROMPT = (
    "You are a lively, sharp chess commentator for a YouTube tournament between AI models. One AI is thinking about "
    "its move right now and you can read its live thinking. Write ONE spoken line of 15 to 30 words, present tense, "
    "starting with the player's name: what it is weighing (candidate moves, a threat it worries about, its plan). "
    "Never say it has played a move. Plain words only: no markdown, no lists, no dashes, no emojis, no move numbers, "
    "write moves the way they are spoken (Knight to f3, Bishop takes e5). Do not invent anything the thinking does not say."
)
MARK_SCORE = {"??": 6.0, "?": 4.0, "?!": 1.5, "!": 3.0}


def next_event(state: dict, done: set) -> dict | None:
    """The next big moment to announce (opening hook, knockouts, an Armageddon decider, the final, the champion)."""
    games = state.get("games") or {}
    names = [spoken(p["name"]) for p in state.get("players") or []]
    fmt = state.get("format") or {}
    ko = state.get("knockout") or {}
    live = [g for g in games.values() if g.get("status") == "live"]
    started = any(r.get("status") == "finished" for r in state.get("rounds") or [])
    if "opening" not in done and live and not started and not any(g.get("result", "*") != "*" for g in games.values()):
        rr = fmt.get("rr_rounds")
        how = (f"a round robin of {rr} rounds where everyone plays everyone, then the top {fmt.get('ko_size', 4)} "
               "go to knockout semifinals and a final, and a drawn knockout game goes to an Armageddon decider") if rr else "a Swiss tournament"
        return {"key": "opening", "game": live[0]["id"],
                "facts": f"The tournament is starting. {len(names)} AI players: {', '.join(names)}. Format: {how}. "
                         "Only one of them will be crowned champion. Open the show."}
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
    return {"key": f"round-{rnd['round']}", "game": lead["id"], "words": "40 to 60",
            "facts": f"Round {rnd['round']}" + (f" of {rr}" if rr else "") + " is starting." + cut
                     + " Standings now: " + "; ".join(_record(r) for r in table) + ". Pairings: " + "; ".join(pairs)
                     + ". Preview the round like a TV host: open with the biggest storyline (the leader, an unbeaten "
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
    return SPOKEN.get(name, name)


def _key() -> str | None:
    value = os.environ.get("OPENROUTER_API_KEY")
    if value:
        return value
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                return str(winreg.QueryValueEx(handle, "OPENROUTER_API_KEY")[0])
        except OSError:
            return None
    return None


def build_context(game: dict, annotations: dict, from_ply: int, switched: bool = False, leaders: str = "") -> str:
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
        reason = (move.get("comment") or "").strip()
        lines.append(f"- {mover} ({move['side']}) played {move['san']}{mark}"
                     + (f" ({mark} from Stockfish)" if mark else "")
                     + (f". Its own reason: {reason[:220]}" if reason else ""))
    if game.get("status") != "live" and game.get("termination"):
        lines.append(f"The game is over: {game.get('result')} - {game['termination']}.")
    return "\n".join(lines)


def clean_line(text: str) -> str:
    text = re.sub(r"\s*[–—]\s*", ", ", re.sub(r"[*_#`]", "", text or ""))
    return " ".join(text.split())[:400]


class Commentator:
    def __init__(self, state_path: Path, out_dir: Path, log=print, budget_usd: float | None = None) -> None:
        self.state_path = Path(state_path)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.budget_usd = float(os.environ.get("COMMENTARY_BUDGET_USD",
                                             DEFAULT_BUDGET_USD if budget_usd is None else budget_usd))
        self.ledger = self.out_dir / "commentary-ledger.json"
        saved = self._load_ledger()
        self.spent_usd = float(saved.get("spent_usd", 0.0))
        self._clips: dict[str, list[dict]] = saved.get("clips", {})
        self._seq = max([c["seq"] for clips in self._clips.values() for c in clips] or [0])
        self._done_ply: dict[str, int] = {g: max(c["ply"] for c in clips) for g, clips in self._clips.items() if clips}
        self._focus: str | None = None
        self._last_game: str | None = None
        self._last_poll = 0.0
        self._events_done: set = set(saved.get("events", []))
        self.always = os.environ.get("COMMENTARY_ALWAYS", "").strip() in {"1", "true", "yes"}
        self._last_said: dict[str, float] = {}
        self._busy_until = 0.0
        self._quiet_from = 0.0    # when the last line finishes playing (about)
        self._thought: set = set()  # (game, ply) whose live thinking was already read out
        self.cost_async = True    # look the TTS cost up after the clip is published (tests: False)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.http = self._http  # replaceable in tests

    # ------------------------------------------------------------ viewer API
    def start(self) -> None:
        if not _key():
            self.log("commentary: OPENROUTER_API_KEY is not set; commentary stays off")
            return
        threading.Thread(target=self._loop, daemon=True).start()
        self.log(f"commentary: on ({TEXT_MODEL} + {TTS_MODEL}, budget ${self.budget_usd:.2f}, spent ${self.spent_usd:.4f})")

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
                self.log(f"commentary: {type(exc).__name__}: {exc}")
                time.sleep(5)

    def tick(self) -> dict | None:
        if self.spent_usd >= self.budget_usd or time.time() < self._busy_until:
            return None
        if not self.always and time.time() - self._last_poll > LISTENER_SECONDS:
            return None  # nobody is listening
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        games = state.get("games") or {}
        moving = time.time() - (state.get("updated_epoch_ms") or 0) / 1000 < LIVE_STATE_SECONDS
        event = next_event(state, self._events_done)
        if event and event["key"] in {"opening"} | {f"round-{state.get('current_round')}"} and not moving:
            event = None  # a paused round looks "live" with no moves: preview it when it really starts
        if event and event.get("game") in games:
            return self._emit_event(event, games[event["game"]])
        all_marks = self._annotations(state)
        game_id = self.pick(state, all_marks)
        if game_id is None:
            return self._think_line(state, all_marks) if moving else None
        game = games[game_id]
        plies = len(game.get("moves") or [])
        done = self._done_ply.get(game_id, 0)
        finished = game.get("status") != "live" and game.get("result", "*") != "*"
        annotations = all_marks.get(game_id, {})
        top = [r for r in (state.get("standings") or []) if r.get("played")][:3]
        leaders = "; ".join(f"{spoken(r['name'])} {r['points']:g} point{'' if r['points'] == 1 else 's'}" for r in top)
        context = build_context(game, annotations, done + 1, switched=game_id != self._last_game, leaders=leaders)
        text = clean_line(self._write_line(context))
        if not text:
            return None
        clip = self._publish(game_id, text, plies, {"final": finished})
        self._done_ply[game_id] = plies
        self.log(f"commentary {game_id} ply {plies}: {text} (total ${self.spent_usd:.4f})")
        return clip

    def _publish(self, game_id: str, text: str, ply: int, extra: dict) -> dict:
        """Voice a line and hand it to the viewer at once; the TTS cost is booked after (not on the clock)."""
        pcm, generation_id = self._speak(text)
        with self._lock:
            self._seq += 1
            name = f"clip-{self._seq}.wav"
            with wave.open(str(self.out_dir / name), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(SAMPLE_RATE)
                out.writeframes(pcm)
            seconds = len(pcm) / (2 * SAMPLE_RATE)
            now = time.time()
            clip = {"seq": self._seq, "ply": ply, "text": text, "audio": name, "seconds": round(seconds, 1),
                    "t": round(now, 2), **extra}
            self._clips.setdefault(game_id, []).append(clip)
            self._last_game = game_id
            self._last_said[game_id] = now
            # The next line is written while this one plays, so it is ready as this one ends.
            self._busy_until = now + max(MIN_GAP_SECONDS, seconds - LEAD_SECONDS)
            self._quiet_from = now + seconds + 1.5
            self._save_ledger()
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
        body = {"model": TEXT_MODEL, "max_tokens": 260, "temperature": 0.9,
                "messages": [{"role": "system", "content": EVENT_PROMPT.format(words=event.get("words", "25 to 45"))},
                             {"role": "user", "content": event["facts"]}]}
        data, _headers = self.http("/chat/completions", body)
        reply = json.loads(data)
        self.spent_usd += float((reply.get("usage") or {}).get("cost") or 0.0)
        text = clean_line(((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        if not text:
            return None
        self._events_done.add(event["key"])
        clip = self._publish(game["id"], text, len(game.get("moves") or []),
                             {"final": event["key"] == "champion", "event": event["key"]})
        self.log(f"commentary event {event['key']}: {text} (total ${self.spent_usd:.4f})")
        return clip

    def _think_line(self, state: dict, all_marks: dict) -> dict | None:
        """No new move anywhere and the line before has finished: read what a model is weighing right now."""
        if time.time() - self._quiet_from < FILLER_GAP_SECONDS:
            return None
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
        body = {"model": TEXT_MODEL, "max_tokens": 120, "temperature": 0.8,
                "messages": [{"role": "system", "content": THINKING_PROMPT}, {"role": "user", "content": context}]}
        data, _headers = self.http("/chat/completions", body)
        reply = json.loads(data)
        self.spent_usd += float((reply.get("usage") or {}).get("cost") or 0.0)
        text = clean_line(((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        if not text:
            return None
        clip = self._publish(best, text, plies, {"final": False, "thinking": True})
        self.log(f"commentary {best} thinking ply {plies + 1}: {text} (total ${self.spent_usd:.4f})")
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
            idle = now - self._last_said.get(game_id, now - 180)
            score = interest(game, all_marks.get(game_id, {}), ranks, done, self._last_game, idle)
            if result_due:
                score += 8.0 if game.get("result") in ("1-0", "0-1") else 5.0
            if score > best_score:
                best, best_score = game_id, score
        return best

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
        sidecar = self.state_path.with_name(f"{state.get('id', '')}-annotations.json")
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            return data.get("annotations", data) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_line(self, context: str) -> str:
        body = {"model": TEXT_MODEL, "max_tokens": 120, "temperature": 0.8,
                "messages": [{"role": "system", "content": COMMENTATOR_PROMPT}, {"role": "user", "content": context}]}
        data, _headers = self.http("/chat/completions", body)
        reply = json.loads(data)
        self.spent_usd += float((reply.get("usage") or {}).get("cost") or 0.0)
        return ((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    def _speak(self, text: str) -> tuple[bytes, str | None]:
        # Only the line itself is spoken; the delivery style goes in `instructions` (2026-10-06: a style prefix
        # inside `input` was sometimes read aloud, "say it like an excited chess commentator...").
        body = {"model": TTS_MODEL, "input": text, "instructions": TTS_STYLE, "voice": TTS_VOICE, "response_format": "pcm"}
        pcm, headers = self.http("/audio/speech", body)
        # The speech endpoint returns raw audio; its cost is read back later from the generation record.
        return pcm, headers.get("X-Generation-Id") or headers.get("x-generation-id")

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
        self.ledger.write_text(json.dumps({"spent_usd": round(self.spent_usd, 6), "clips": self._clips,
                                           "events": sorted(self._events_done)}, indent=1), encoding="utf-8")
