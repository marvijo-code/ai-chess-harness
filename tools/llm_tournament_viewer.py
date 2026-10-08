"""Live web view of an LLM Swiss tournament written by tools/play_llm_swiss.py.

    python tools/llm_tournament_viewer.py --port 8770 [--state out/live/<slug>-tournament.json] [--commentary]
    python tools/llm_tournament_viewer.py --port 8770 --follow out/live/current.json   # forever tournament

Without --state it follows the newest out/live/*-tournament.json. With --follow it serves the state the pointer
file names ({"state_path", "id", "number"}, written by tools/ai_chess_forever.py) and switches to the next
tournament as soon as the pointer changes, without a restart. The page polls
/api/tournament once a second and shows every board of the current round, the Elo
standings and all rounds; click a finished game to replay it with the arrow keys,
click a board header (or Focus) to watch that one board large.

Viewer-only Stockfish work (the AI players never see any of it):
  * Analyzer   - the eval bar and PV line of the shown positions.
  * Annotator  - a second Stockfish process that marks every played move with the
                 standard ?? ? ?! ! symbols, cached in <slug>-annotations.json.
--commentary starts tools/llm_commentary.py (Commentator) and serves its clips.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(__file__).resolve().parent
LIVE_DIR = ROOT / "out" / "live"
VIEWER_VERSION = str(int(Path(__file__).stat().st_mtime))

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Chess Swiss</title>
<style>
:root {
  --bg: #0e1013; --panel: #171a1f; --panel-2: #1e2229; --line: #2a2f38; --text: #eef1f5; --muted: #9aa3af;
  --accent: #5b9dff; --ok: #34c77b; --warn: #f5b942; --bad: #ff5d5d; --sq-light: #eef0f3; --sq-dark: #a9afb9;
  --hl: rgba(91, 157, 255, .42);
  --nag-bad: #f07a7a; --nag-dubious: #e3b155; --nag-good: #63cf92;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.4 "Segoe UI", system-ui, -apple-system, sans-serif; }
body { min-height: 100vh; overflow-x: hidden; }
header { display: flex; align-items: center; gap: 16px; padding: 14px 22px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
h1 { font-size: 22px; margin: 0; letter-spacing: .2px; }
.chips { display: flex; gap: 8px; flex-wrap: wrap; min-width: 0; }
.chip { background: var(--panel-2); border: 1px solid var(--line); border-radius: 999px; padding: 3px 11px; font-size: 13px; color: var(--muted); white-space: nowrap; }
.chip b { color: var(--text); font-weight: 600; }
.chip.live { color: var(--ok); border-color: rgba(52, 199, 123, .45); }
.chip.done { color: var(--warn); border-color: rgba(245, 185, 66, .45); }
main { display: grid; grid-template-columns: minmax(0, 1fr) 470px; gap: 18px; padding: 18px 22px; align-items: start; }
.boards { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 18px; min-width: 0; align-items: start; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 14px; min-width: 0; }
/* The whole game card (bars, board, comment, moves, collapsed thinking) must fit one 1080p screen. */
.boards > .card[data-game] { width: 100%; max-width: max(360px, calc(100vh - 575px)); justify-self: center; }
.card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin: 0 0 10px; font-weight: 600; }
.ladder-tag { color: var(--muted); font-weight: 400; white-space: nowrap; }
#agents .ag { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 10px; padding: 6px 0; border-top: 1px solid var(--line); }
#agents .ag:first-child { border-top: 0; }
#agents .ag-n { font-weight: 600; min-width: 0; overflow-wrap: anywhere; }
#agents .ag-c { color: var(--muted); font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
#agents .ag-note { grid-column: 1 / -1; color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
/* Stream mode (?stream=1 or --stream-layout): exactly one 1920x1080 screen, nothing below the fold.
   The boards of the round side by side (two at most), the side column keeps the standings (with the
   Stockfish depth), the notes and cache card and the bracket; rounds and rules are left out. */
body.stream { height: 100vh; overflow: hidden; }
body.stream main { height: calc(100vh - var(--chrome-h, 62px)); grid-template-columns: minmax(0, 1fr) 440px; padding: 14px 22px; overflow: hidden; }
body.stream .boards { grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); height: 100%; align-content: start; overflow: hidden; }
body.stream .boards > .card[data-game] { max-width: max(360px, calc(100vh - 600px)); }
body.stream .boards > .card[data-game]:nth-child(n+3) { display: none; }
body.stream .side { position: static; max-height: none; height: 100%; overflow: hidden; display: flex; flex-direction: column; gap: 14px; }
body.stream .side > .card { flex: 0 0 auto; }
body.stream .side > .card:not(#standingsCard):not(#agentsCard):not(#bracketCard) { display: none; }
body.stream #agentsCard { flex: 0 0 auto; }   /* never clipped: fitStream() shrinks the whole column instead */
body.stream #agents .ag-note { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
body.stream .ticker { display: none !important; }
body.stream .champ-btn { display: none; }
body.stream td .route { display: none; }
body.stream .side > .card, body.stream .boards { transform-origin: top left; }
.game-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; gap: 6px 8px; cursor: pointer; min-width: 0; flex-wrap: wrap; }
.card.focused .game-head { cursor: default; }
.game-head .tag { font-size: 12px; color: var(--muted); min-width: 0; flex: 1 1 140px; white-space: normal; overflow-wrap: anywhere; line-height: 1.25; }
.game-head .tag.ko { color: #ffd479; font-weight: 600; font-size: 12.5px; }
.game-head .head-right { display: flex; gap: 6px; align-items: center; flex: none; }
.result-pill { font-weight: 700; font-size: 13px; padding: 2px 10px; border-radius: 999px; background: var(--panel-2); border: 1px solid var(--line); white-space: nowrap; }
.result-pill.live { color: var(--ok); border-color: rgba(52, 199, 123, .45); }
.pbar { display: flex; align-items: center; gap: 10px; padding: 7px 10px; border-radius: 10px; background: var(--panel-2); margin: 6px 0; min-width: 0; }
.pbar .dot { width: 14px; height: 14px; border-radius: 50%; flex: none; border: 1px solid #666; }
.pbar .dot.w { background: #fff; } .pbar .dot.b { background: #111; }
.pbar .name { font-weight: 600; flex: 1; min-width: 0; white-space: normal; overflow-wrap: anywhere; line-height: 1.2; }
.pbar .elo { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
.pbar .clock { font: 600 18px/1 ui-monospace, "Cascadia Mono", Consolas, monospace; padding: 4px 8px; border-radius: 6px; background: #0b0d10; min-width: 64px; text-align: center; }
.pbar.to-move .clock { background: #fff; color: #000; }
.pbar .think { font-size: 12px; color: var(--accent); min-width: 88px; text-align: right; font-variant-numeric: tabular-nums; }
.board { display: grid; grid-template-columns: repeat(8, minmax(0, 1fr)); grid-template-rows: repeat(8, minmax(0, 1fr)); aspect-ratio: 1; width: 100%; border-radius: 6px; overflow: hidden; user-select: none; }
.sq { position: relative; display: flex; align-items: center; justify-content: center; container-type: size; min-width: 0; min-height: 0; overflow: hidden; }
.sq.l { background: var(--sq-light); } .sq.d { background: var(--sq-dark); }
.sq.hl::after { content: ""; position: absolute; inset: 0; background: var(--hl); }
.pc { font-size: 80cqh; line-height: 1; position: relative; z-index: 1; font-family: "Segoe UI Symbol", "DejaVu Sans", sans-serif; }
.pc.w { color: #fff; text-shadow: 0 0 1px #000, 0 0 1px #000, 0 0 2px #000, 1px 1px 0 #000, -1px -1px 0 #000, 1px -1px 0 #000, -1px 1px 0 #000; }
.pc.b { color: #111; }
.coord { position: absolute; font-size: 10px; color: #555; z-index: 2; }
.coord.f { right: 3px; bottom: 1px; } .coord.r { left: 3px; top: 1px; }
.board-wrap { display: grid; grid-template-columns: 16px minmax(0, 1fr); gap: 8px; align-items: stretch; }
.board-wrap.no-eval { grid-template-columns: minmax(0, 1fr); }
.evalbar { position: relative; border-radius: 5px; overflow: hidden; background: #111; border: 1px solid var(--line); }
.evalbar .white { position: absolute; left: 0; right: 0; bottom: 0; height: 50%; background: #f2f2f2; transition: height .6s ease; }
.evalbar .mid { position: absolute; left: 0; right: 0; top: 50%; height: 1px; background: rgba(255, 85, 85, .7); }
.evalline { margin-top: 8px; padding: 7px 10px; border-radius: 10px; background: #12161c; border: 1px solid var(--line); font-size: 13px; color: var(--muted); display: flex; gap: 10px; align-items: baseline; min-width: 0; }
.evalline .score { font: 700 16px/1 ui-monospace, "Cascadia Mono", Consolas, monospace; color: var(--text); min-width: 58px; }
.evalline .pv { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1; }
.evalline .eng { white-space: nowrap; }
.chip.btn { cursor: pointer; user-select: none; } .chip.btn.on { color: var(--accent); border-color: rgba(91, 157, 255, .5); }
.chip.btn.wait { color: var(--warn); border-color: rgba(245, 185, 66, .45); }
.comment { margin-top: 10px; padding: 9px 11px; background: var(--panel-2); border-radius: 10px; font-size: 14px; min-height: 58px; overflow-wrap: anywhere; }
.comment .who { color: var(--muted); font-size: 12px; margin-bottom: 2px; }
.caption { margin: 6px 0 0; padding: 6px 10px; border-radius: 10px 10px 10px 2px; background: rgba(91, 157, 255, .13); border: 1px solid rgba(91, 157, 255, .35); font-size: 12.5px; line-height: 1.4; color: var(--text); overflow-wrap: anywhere; }
.card.on-air { border-color: rgba(91, 157, 255, .65); box-shadow: 0 0 0 1px rgba(91, 157, 255, .35); transition: border-color .3s ease, box-shadow .3s ease; }
.air-tag { font-size: 11.5px; font-weight: 600; color: var(--accent); background: rgba(91, 157, 255, .13); border: 1px solid rgba(91, 157, 255, .4); border-radius: 999px; padding: 2px 8px; white-space: nowrap; }
.caption::before { content: "Commentary: "; color: var(--accent); font-weight: 600; }
.caption .cap-on { display: inline-block; margin-right: 6px; padding: 0 7px; border-radius: 999px; background: rgba(91, 157, 255, .22); color: #cfe0ff;
  font: 600 11.5px/1.6 ui-monospace, "Cascadia Mono", Consolas, monospace; white-space: nowrap; }
.moves { position: relative; margin-top: 8px; padding: 6px 9px; border-radius: 10px; background: #12161c; border: 1px solid var(--line); font: 13px/1.65 ui-monospace, "Cascadia Mono", Consolas, monospace; color: var(--muted); max-height: 110px; overflow-y: auto; overscroll-behavior: contain; word-break: break-word; }
.moves .mv { cursor: pointer; border-radius: 4px; padding: 0 2px; }
.moves .mv:hover { color: var(--text); }
.moves .cur { color: var(--text); background: rgba(91, 157, 255, .25); }
.moves .bad { color: var(--bad); }
.moves .num { color: #6b7380; }
.nag { font-weight: 700; margin-left: 1px; }
.nag.blunder, .nag.mistake { color: var(--nag-bad); }
.nag.dubious { color: var(--nag-dubious); }
.nag.good { color: var(--nag-good); }
.nav { display: flex; gap: 6px; margin-top: 8px; align-items: center; flex-wrap: wrap; }
.nav button, .linkbtn { background: var(--panel-2); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 4px 10px; cursor: pointer; font: inherit; font-size: 13px; white-space: nowrap; }
.nav button:hover, .linkbtn:hover { border-color: var(--accent); }
.linkbtn.small { padding: 2px 9px; font-size: 12px; }
.nav .ply { color: var(--muted); font-size: 12px; margin-left: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th { text-align: left; color: var(--muted); font-size: 12px; font-weight: 600; padding: 5px 6px; border-bottom: 1px solid var(--line); white-space: nowrap; }
td { padding: 7px 6px; border-bottom: 1px solid var(--line); white-space: nowrap; }
td.player { font-weight: 600; white-space: normal; }
td .route { display: block; color: var(--muted); font-size: 11px; font-weight: 400; }
.num { text-align: right; }
.up { color: var(--ok); } .down { color: var(--bad); }
.rank1 td { background: rgba(245, 185, 66, .08); }
.side { display: grid; gap: 18px; min-width: 0; }
.round { margin-bottom: 10px; }
.round-title { display: flex; justify-content: space-between; font-size: 13px; color: var(--muted); margin-bottom: 4px; }
.pair { display: grid; grid-template-columns: minmax(0, 1fr) 62px minmax(0, 1fr); gap: 6px; align-items: center; padding: 5px 8px; border-radius: 8px; cursor: pointer; font-size: 14px; }
.pair:hover { background: var(--panel-2); }
.pair.sel { background: rgba(91, 157, 255, .16); }
.pair .w { text-align: right; min-width: 0; white-space: normal; overflow-wrap: anywhere; line-height: 1.2; }
.pair .b { min-width: 0; white-space: normal; overflow-wrap: anywhere; line-height: 1.2; }
.pair .r { text-align: center; font-weight: 700; }
.pair .r.live { color: var(--ok); font-size: 12px; }
.pair .why { grid-column: 1 / -1; color: var(--muted); font-size: 11.5px; text-align: center; margin-top: -2px; }
.bye { font-size: 12.5px; color: var(--muted); padding: 2px 8px; }
.empty { color: var(--muted); padding: 30px; text-align: center; }
.rules { color: var(--muted); font-size: 13px; line-height: 1.55; margin: 0; padding-left: 18px; }
/* Thinking: what the model streams while it decides (collapsible, per card). */
.thinking { margin-top: 8px; border-radius: 10px; background: #12161c; border: 1px solid var(--line); min-width: 0; display: flex; flex-direction: column; }
.think-head { display: flex; align-items: center; gap: 8px; width: 100%; min-width: 0; padding: 6px 10px; background: none; border: 0; border-radius: 10px; color: var(--muted); font: inherit; font-size: 12.5px; text-align: left; cursor: pointer; }
.think-head:hover { color: var(--text); }
.think-head:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.think-head .chev { flex: none; width: 0; height: 0; border-left: 5px solid currentColor; border-top: 4px solid transparent; border-bottom: 4px solid transparent; transition: transform .15s ease; }
.thinking.open .think-head .chev { transform: rotate(90deg); }
.think-head .label { flex: 1 1 auto; min-width: 0; white-space: normal; overflow-wrap: anywhere; line-height: 1.25; font-weight: 600; }
.think-head .meta { flex: none; display: flex; align-items: center; gap: 6px; font-size: 11.5px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.think-head .meta .live { color: var(--ok); }
.think-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ok); animation: think-pulse 1.4s ease-in-out infinite; }
@keyframes think-pulse { 0%, 100% { opacity: .25; transform: scale(.8); } 50% { opacity: 1; transform: scale(1); } }
@media (prefers-reduced-motion: reduce) { .think-dot { animation: none; } }
.think-body { border-top: 1px solid var(--line); padding: 6px 10px 8px; max-height: 240px; min-height: 0; overflow-y: auto; overflow-x: hidden; overscroll-behavior: contain;
  font: 11.5px/1.5 ui-monospace, "Cascadia Mono", Consolas, monospace; color: #8a93a0; white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; }
.think-body[hidden] { display: none; }
.think-body .think-note { font-family: "Segoe UI", system-ui, sans-serif; font-style: italic; color: var(--muted); white-space: normal; }
.think-body .think-clip { display: block; font-family: "Segoe UI", system-ui, sans-serif; font-size: 11px; color: #6b7380; margin-bottom: 4px; white-space: normal; }
/* Focus mode: one board, as large as the viewport allows, with its info column beside it. */
body.focus-mode main { grid-template-columns: minmax(0, 1fr); }
body.focus-mode .side { display: none; }
body.focus-mode .boards { display: block; }
.boards > .card.focused { max-width: none; display: grid; gap: 4px 22px; align-items: start;
  --fboard: max(340px, min(calc(100vh - 290px - var(--hdr-extra, 0px)), calc(100vw - 560px)));
  grid-template-columns: var(--fboard) minmax(300px, 1fr); grid-template-areas: "head head" "boardcol infocol"; }
.card.focused .game-head { grid-area: head; }
.card.focused .boardcol { grid-area: boardcol; min-width: 0; }
.card.focused .infocol { grid-area: infocol; min-width: 0; display: flex; flex-direction: column; height: calc(var(--fboard) + 64px); }
.card.focused .infocol .evalline { margin-top: 6px; }
.card.focused .infocol > * { flex-shrink: 0; }   /* only the move list and the thinking panel give up height */
.card.focused .moves { max-height: none; flex: 1 1 auto; min-height: 140px; font-size: 14px; }
.card.focused .infocol > .thinking { flex: 0 1 auto; min-height: 34px; overflow: hidden; }
.card.focused .think-body { max-height: 45vh; font-size: 12px; flex: 1 1 auto; min-height: 0; }
@media (max-height: 820px) {
  .card.focused .moves { min-height: 56px; }
  .card.focused .infocol .comment { min-height: 0; margin-top: 6px; padding: 6px 10px; font-size: 13px; }
  .card.focused .infocol [data-part="result"] .who { display: inline; margin-right: 8px; }
  .card.focused .infocol { overflow-y: auto; overscroll-behavior: contain; }   /* last resort: scroll, never cut */
  .card.focused .lb-r { padding-top: 2px; padding-bottom: 2px; }
}
.card.switch-in { animation: switch-in .45s ease both; }
@keyframes switch-in { from { opacity: .2; transform: translateY(8px); } to { opacity: 1; transform: none; } }
/* ---- round robin + knockouts ---------------------------------------------------------------- */
.chip.stage { color: var(--ok); border-color: rgba(52, 199, 123, .45); }
.chip.stage.ko { color: #ffd479; border-color: rgba(245, 185, 66, .6); background: rgba(245, 185, 66, .09); font-weight: 600; }
.chip.stage.champ b { color: #ffd479; }
.chip .ico { display: inline-block; width: 14px; height: 14px; vertical-align: -2px; margin-right: 5px; }
.arma-band { flex: 1 0 100%; order: 3; display: flex; align-items: center; gap: 4px 10px; flex-wrap: wrap; padding: 5px 10px; border-radius: 8px;
  background: linear-gradient(90deg, rgba(255, 93, 93, .2), rgba(245, 185, 66, .1)); border: 1px solid rgba(255, 93, 93, .5); font-size: 12px; color: var(--text); }
.arma-band b { color: #ff8a7a; letter-spacing: .14em; font-size: 12.5px; }
.arma-band .odds { font-weight: 600; }
.arma-band .clk { color: var(--muted); }
.pair .lbl { grid-column: 1 / -1; text-align: center; font-size: 11.5px; color: #ffd479; font-weight: 600; }
.pair .lbl .a { color: #ff8a7a; letter-spacing: .08em; margin-left: 4px; }
.side > .card { order: 2; }
.side > #bracketCard { order: 1; }
.side.ko-first > #bracketCard { order: 0; }
.side > #standingsCard { order: 0; }
.side.ko-first > #standingsCard { order: 1; }
.bk-note { font-size: 12px; color: var(--muted); margin: -4px 0 10px; }
.bk-semis { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.bk-match { background: var(--panel-2); border: 1px solid var(--line); border-radius: 10px; padding: 8px 8px 7px; min-width: 0; }
.bk-match.click { cursor: pointer; } .bk-match.click:hover { border-color: var(--accent); }
.bk-match.live { border-color: rgba(52, 199, 123, .55); }
.bk-match.arma { border-color: rgba(255, 93, 93, .6); box-shadow: inset 0 0 0 1px rgba(255, 93, 93, .18); }
.bk-match.done { border-color: rgba(245, 185, 66, .35); }
.bk-head { display: flex; justify-content: space-between; align-items: baseline; gap: 2px 8px; flex-wrap: wrap; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 4px; }
.bk-head .t { color: var(--text); font-weight: 700; }
.bk-state.live { color: var(--ok); font-weight: 700; }
.bk-state.arma { color: #ff8a7a; font-weight: 700; }
.bk-state.won { color: #ffd479; }
.bk-p { display: grid; grid-template-columns: 20px minmax(0, 1fr) auto; gap: 6px; align-items: center; padding: 4px 4px; border-radius: 7px; font-size: 13.5px; }
.bk-seed { font-size: 11px; color: var(--muted); text-align: center; border: 1px solid var(--line); border-radius: 5px; line-height: 16px; font-variant-numeric: tabular-nums; }
.bk-name { min-width: 0; overflow-wrap: anywhere; line-height: 1.2; font-weight: 600; }
.bk-name.tbd { color: var(--muted); font-weight: 400; font-style: italic; font-size: 12.5px; }
.bk-res { display: flex; gap: 3px; font: 700 12.5px/1 "Segoe UI", system-ui, sans-serif; font-variant-numeric: tabular-nums; }
.bk-res span { min-width: 17px; text-align: center; padding: 3px 3px; border-radius: 4px; background: #12161c; color: var(--muted); }
.bk-res span.a { color: #ff8a7a; box-shadow: inset 0 0 0 1px rgba(255, 93, 93, .45); }
.bk-res span.lv { color: var(--ok); }
.bk-p.win { background: rgba(245, 185, 66, .15); }
.bk-p.win .bk-name { color: #ffd479; }
.bk-p.out { opacity: .5; }
.bk-why { font-size: 11px; color: var(--muted); margin-top: 4px; overflow-wrap: anywhere; }
.bk-join { position: relative; height: 16px; margin: 0 25%; border: 2px solid var(--line); border-top: 0; border-radius: 0 0 9px 9px; }
.bk-join::after { content: ""; position: absolute; left: calc(50% - 1px); top: 100%; height: 10px; border-left: 2px solid var(--line); }
.bk-final { width: min(100%, 300px); margin: 10px auto 0; }
.bk-final.bk-match { border-width: 1px; background: linear-gradient(180deg, rgba(245, 185, 66, .08), var(--panel-2)); }
.bk-champ { display: flex; align-items: center; justify-content: center; gap: 10px; width: min(100%, 300px); margin: 8px auto 0; padding: 8px 10px; border-radius: 10px; border: 1px dashed var(--line); color: var(--muted); font-size: 13px; text-align: center; }
.bk-champ svg { width: 26px; height: 26px; flex: none; }
.bk-champ.won { border: 1px solid rgba(245, 185, 66, .7); background: rgba(245, 185, 66, .14); color: var(--text); }
.bk-champ.won b { color: #ffd479; font-size: 16px; overflow-wrap: anywhere; }
.bk-third { margin-top: 10px; }
.bk-third .bk-match { opacity: .92; }
/* Champion moment: full-screen celebration over the boards. */
.champ-overlay { position: fixed; inset: 0; z-index: 50; display: flex; align-items: center; justify-content: center; padding: 24px 16px; overflow-y: auto;
  background: radial-gradient(ellipse at 50% 32%, rgba(245, 185, 66, .2), rgba(14, 16, 19, .93) 62%); backdrop-filter: blur(3px); }
.champ-overlay[hidden] { display: none; }
.champ-overlay canvas { position: fixed; inset: 0; width: 100%; height: 100%; pointer-events: none; z-index: 1; transition: opacity 1.2s ease; }
.champ-card { position: relative; z-index: 2; text-align: center; width: min(940px, 100%); margin: auto; padding: clamp(20px, 4vh, 40px) clamp(16px, 3vw, 36px) clamp(18px, 3vh, 30px); border-radius: 22px;
  background: rgba(23, 26, 31, .88); border: 1px solid rgba(245, 185, 66, .5); box-shadow: 0 30px 90px rgba(0, 0, 0, .6), inset 0 0 0 1px rgba(245, 185, 66, .14); }
.champ-crown { width: clamp(84px, 13vh, 150px); height: auto; display: block; margin: 0 auto 6px; filter: drop-shadow(0 8px 26px rgba(245, 185, 66, .55)); }
.champ-kicker { text-transform: uppercase; letter-spacing: .32em; color: #ffd479; font-weight: 700; font-size: clamp(12px, 1.7vh, 18px); }
.champ-name { font-size: clamp(30px, min(7.5vh, 10.5vw), 88px); font-weight: 800; line-height: 1.05; margin: 10px 0 4px; overflow-wrap: anywhere;
  background: linear-gradient(180deg, #fff6d2 0%, #f5b942 62%, #c98a1a 100%); -webkit-background-clip: text; background-clip: text; color: transparent; }
.champ-sub { font-size: clamp(18px, 2.7vh, 30px); font-weight: 600; }
.champ-seed { color: var(--muted); font-size: clamp(13px, 1.7vh, 16px); margin-top: 4px; }
.podium { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; margin: clamp(14px, 2.6vh, 26px) 0 12px; }
.podium > div { background: var(--panel-2); border: 1px solid var(--line); border-radius: 12px; padding: 10px 14px; min-width: 0; }
.podium .lbl { font-size: 12px; text-transform: uppercase; letter-spacing: .12em; color: var(--muted); }
.podium .nm { font-size: clamp(17px, 2.3vh, 22px); font-weight: 700; overflow-wrap: anywhere; }
.podium .silver .lbl { color: #cfd6df; } .podium .bronze .lbl { color: #d9a26b; }
.champ-line { color: var(--muted); font-size: clamp(13px, 1.8vh, 16px); margin: 0 0 16px; overflow-wrap: anywhere; }
.champ-btn { font: inherit; font-size: 15px; padding: 9px 22px; border-radius: 999px; background: #f5b942; color: #15120a; border: 0; font-weight: 700; cursor: pointer; }
.champ-btn:hover { background: #ffd479; }
.champ-overlay.play .champ-crown { animation: crown-drop 1s cubic-bezier(.2, 1.45, .4, 1) .15s both; }
.champ-overlay.play .champ-kicker { animation: rise .6s ease .6s both; }
.champ-overlay.play .champ-name { animation: name-in .9s cubic-bezier(.2, 1.3, .4, 1) .85s both; }
.champ-overlay.play .champ-sub, .champ-overlay.play .champ-seed { animation: rise .6s ease 1.4s both; }
.champ-overlay.play .podium > div { animation: rise .6s ease both; }
.champ-overlay.play .podium > div:nth-child(1) { animation-delay: 1.8s; } .champ-overlay.play .podium > div:nth-child(2) { animation-delay: 2s; }
.champ-overlay.play .champ-line, .champ-overlay.play .champ-btn { animation: rise .6s ease 2.3s both; }
.champ-overlay.play .champ-card { animation: card-in .6s ease both; }
@keyframes crown-drop { from { opacity: 0; transform: translateY(-80px) rotate(-14deg) scale(.6); } to { opacity: 1; transform: none; } }
@keyframes name-in { from { opacity: 0; transform: scale(.6); letter-spacing: .2em; } to { opacity: 1; transform: none; } }
@keyframes rise { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }
@keyframes card-in { from { opacity: 0; transform: scale(.94); } to { opacity: 1; transform: none; } }
/* Opening hook: a 6 second title card over the page. */
.intro { position: fixed; inset: 0; z-index: 60; display: flex; flex-direction: column; align-items: center; justify-content: safe center; gap: clamp(12px, 3vh, 30px);
  padding: 24px 16px 44px; overflow: hidden; cursor: pointer; background: radial-gradient(ellipse at 50% 28%, #1c2945 0%, #11151c 48%, #0b0d10 100%);
  animation: intro-out .7s ease 5.3s forwards; }
.intro.leaving { animation: intro-out .35s ease forwards; }
@keyframes intro-out { to { opacity: 0; visibility: hidden; } }
.intro-kicker { letter-spacing: .34em; text-transform: uppercase; color: var(--accent); font-weight: 700; font-size: clamp(12px, 1.7vh, 17px); text-align: center; animation: rise .5s ease .05s both; }
.intro-head { font-size: clamp(38px, min(11.5vh, 12vw), 140px); font-weight: 900; line-height: .98; text-align: center; margin: 0; letter-spacing: -.02em; }
.intro-head span { display: inline-block; white-space: nowrap; }
.intro-head .a { animation: slam .55s cubic-bezier(.2, 1.5, .4, 1) .2s both; }
.intro-head .b { animation: slam .55s cubic-bezier(.2, 1.5, .4, 1) .7s both; background: linear-gradient(180deg, #fff6d2, #f5b942 65%, #c98a1a);
  -webkit-background-clip: text; background-clip: text; color: transparent; filter: drop-shadow(0 4px 22px rgba(245, 185, 66, .35)); }
@keyframes slam { from { opacity: 0; transform: scale(2.2); } to { opacity: 1; transform: none; } }
.intro-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: clamp(8px, 1.2vh, 12px); width: min(1240px, 100%); }
.intro-p { background: rgba(23, 26, 31, .92); border: 1px solid var(--line); border-radius: 12px; padding: clamp(8px, 1.3vh, 13px) 10px; text-align: center; font-weight: 700;
  font-size: clamp(14px, 2.1vh, 22px); line-height: 1.2; overflow-wrap: anywhere; min-width: 0; display: flex; align-items: center; justify-content: center;
  animation: fly .65s cubic-bezier(.2, 1.15, .4, 1) both; animation-delay: calc(1.1s + var(--i) * .11s); }
@keyframes fly { from { opacity: 0; transform: translate(var(--dx), var(--dy)) rotate(var(--r)) scale(.5); } to { opacity: 1; transform: none; } }
.intro-fmt { display: flex; flex-wrap: wrap; gap: 8px 10px; justify-content: center; align-items: center; font-size: clamp(14px, 2.3vh, 24px); font-weight: 600; max-width: 100%; }
.intro-fmt span { padding: 6px 14px; border-radius: 999px; border: 1px solid var(--line); background: var(--panel-2); animation: rise .45s ease both; animation-delay: calc(2.6s + var(--i) * .32s); }
.intro-fmt .arr { border: 0; background: none; padding: 0; color: var(--muted); }
.intro-fmt .ko { border-color: rgba(245, 185, 66, .6); color: #ffd479; }
.intro-fmt .arma { border-color: rgba(255, 93, 93, .6); color: #ff8a7a; }
.intro-skip { position: absolute; bottom: 14px; right: 18px; font-size: 13px; color: var(--muted); }
.intro-bar { position: absolute; left: 0; bottom: 0; height: 4px; background: linear-gradient(90deg, var(--accent), #f5b942); animation: intro-bar 6s linear both; }
@keyframes intro-bar { from { width: 0; } to { width: 100%; } }
@media (prefers-reduced-motion: reduce) {
  .intro *, .champ-overlay * { animation-duration: .01s !important; animation-delay: 0s !important; }
}
@media (max-width: 700px) { .intro-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
/* Compact bracket beside the big board: focus mode on wide screens only (the side panel is hidden there). */
.mini-bk { display: none; }
@media (min-width: 1280px) { .card.focused .mini-bk:not(:empty) { display: block; flex: none; margin-bottom: 4px; } }
.mb-title { display: flex; justify-content: space-between; align-items: baseline; gap: 2px 10px; flex-wrap: wrap; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); font-weight: 600; margin-bottom: 4px; }
.mb-title .mb-note { text-transform: none; letter-spacing: 0; font-weight: 400; }
.mb-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); gap: 6px; }
@media (max-width: 1599px) { .mb-m.third { display: none; } }
.mb-m { background: var(--panel-2); border: 1px solid var(--line); border-radius: 9px; padding: 5px 6px; min-width: 0; }
.mb-m.click { cursor: pointer; } .mb-m.click:hover { border-color: var(--accent); }
.mb-m.live { border-color: rgba(52, 199, 123, .55); }
.mb-m.arma { border-color: rgba(255, 93, 93, .65); }
.mb-m.done { border-color: rgba(245, 185, 66, .35); }
.mb-m.here { box-shadow: 0 0 0 2px rgba(91, 157, 255, .55); }
.mb-h { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 0 6px; font-size: 10.5px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); margin-bottom: 2px; line-height: 1.3; }
.mb-h .t { color: var(--text); font-weight: 700; }
.mb-h .live { color: var(--ok); font-weight: 700; } .mb-h .arma { color: #ff8a7a; font-weight: 700; } .mb-h .won { color: #ffd479; }
.mb-p { display: grid; grid-template-columns: 18px minmax(0, 1fr); gap: 5px; align-items: center; padding: 2px 2px; border-radius: 6px; font-size: 12.5px; }
.mb-p .bk-seed { font-size: 10.5px; line-height: 15px; }
.mb-n { min-width: 0; overflow-wrap: anywhere; line-height: 1.2; font-weight: 600; }
.mb-n.tbd, .mb-champ .tbd { color: var(--muted); font-weight: 400; font-style: italic; font-size: 12px; }
.mb-p.win { background: rgba(245, 185, 66, .15); } .mb-p.win .mb-n { color: #ffd479; }
.mb-p.out { opacity: .5; }
.mb-champ { display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; gap: 1px; border-style: dashed; }
.mb-champ svg { width: 24px; height: 22px; }
.mb-champ.won { border: 1px solid rgba(245, 185, 66, .7); background: rgba(245, 185, 66, .14); }
.mb-champ.won b { color: #ffd479; font-size: 13.5px; overflow-wrap: anywhere; line-height: 1.2; }
/* Compact leaderboard in focus mode (always visible): a third column on wide screens, under the board elsewhere. */
.lb { display: none; background: #12161c; border: 1px solid var(--line); border-radius: 10px; padding: 7px 8px; min-width: 0; }
.card.focused .lb-under:not(:empty) { display: block; margin-top: 8px; }
@media (min-width: 1280px) {
  .boards > .card.focused { --fboard: max(340px, min(calc(100vh - 290px - var(--hdr-extra, 0px)), calc(100vw - 822px)));
    grid-template-columns: var(--fboard) minmax(300px, 1fr) clamp(240px, 18vw, 300px); grid-template-areas: "head head head" "boardcol infocol lbcol"; }
  .card.focused .lb-under:not(:empty) { display: none; }
  .card.focused .lb-side:not(:empty) { display: flex; flex-direction: column; grid-area: lbcol; height: calc(var(--fboard) + 64px); }
  .card.focused .lb-side .lb-list { flex: 1 1 auto; min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
}
.lb-title { display: flex; justify-content: space-between; align-items: baseline; gap: 2px 8px; flex-wrap: wrap; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); font-weight: 600; margin-bottom: 3px; }
.lb-title .n { text-transform: none; letter-spacing: 0; font-weight: 400; }
.lb-r { display: grid; grid-template-columns: 20px minmax(0, 1fr) 30px 46px; gap: 4px; align-items: center; padding: 3px 4px; border-radius: 6px; font-size: 12.5px; font-variant-numeric: tabular-nums; }
.lb-r.lb-head { color: var(--muted); font-size: 10.5px; text-transform: uppercase; letter-spacing: .05em; padding-top: 0; padding-bottom: 1px; }
.lb-k { color: var(--muted); text-align: center; font-size: 11.5px; }
.lb-n { min-width: 0; overflow-wrap: anywhere; line-height: 1.2; font-weight: 600; }
.lb-n .sd { display: inline-block; font-size: 10px; color: #ffd479; border: 1px solid rgba(245, 185, 66, .5); border-radius: 4px; padding: 0 3px; margin-left: 4px; font-weight: 700; line-height: 1.3; white-space: nowrap; }
.lb-n .cr { display: inline-block; width: 13px; height: 12px; margin-left: 4px; vertical-align: -1px; }
.lb-p { text-align: right; font-weight: 700; }
.lb-e { text-align: right; line-height: 1.1; font-size: 12px; }
.lb-e i { display: block; font-style: normal; font-size: 10.5px; }
.lb-r.zone { background: rgba(245, 185, 66, .06); }
.lb-r.me { background: rgba(91, 157, 255, .22); box-shadow: inset 3px 0 0 var(--accent); }
.lb-r .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; border: 1px solid #777; margin-right: 5px; vertical-align: 0; }
.lb-r .dot.w { background: #fff; } .lb-r .dot.b { background: #111; }
.lb-cut { display: flex; align-items: center; gap: 6px; margin: 3px 0; font-size: 10px; color: #ffd479; text-transform: uppercase; letter-spacing: .06em; white-space: nowrap; }
@media (min-width: 1700px) and (min-height: 900px) {
  .lb-r { font-size: 14px; padding: 4px 5px; grid-template-columns: 22px minmax(0, 1fr) 34px 50px; } .lb-e { font-size: 13px; } .lb-e i { font-size: 11.5px; }
  .lb-title { font-size: 12px; }
}
.lb-cut::before, .lb-cut::after { content: ""; flex: 1; border-top: 1px dashed rgba(245, 185, 66, .55); }
@media (max-width: 1100px) { main { grid-template-columns: 1fr; } }
/* Standings on every view (owner 2026-10-06): the side column stays on screen while the boards scroll;
   where the full table is not on screen (one column, or focus without the leaderboard column) a
   standings strip rides in the sticky header. */
@media (min-width: 1101px) {
  .side { position: sticky; top: 12px; max-height: calc(100vh - 24px); overflow-y: auto; overscroll-behavior: contain; scrollbar-width: thin; }
}
.ticker { display: none; position: sticky; top: 0; z-index: 30; background: var(--bg); border-bottom: 1px solid var(--line); padding: 6px 22px;
  flex-wrap: wrap; align-items: baseline; gap: 3px 12px; font-size: 12.5px; font-variant-numeric: tabular-nums; }
.ticker .tk-h { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; font-weight: 600; }
.ticker .tk { white-space: nowrap; }
.ticker .tk i { font-style: normal; color: var(--muted); margin-right: 3px; }
.ticker .tk b { color: var(--text); margin-left: 4px; }
.ticker .tk.zone b { color: #ffd479; }
.ticker .tk.me { color: #cfe0ff; }
@media (max-width: 1100px) { .ticker:not(:empty) { display: flex; } }
@media (max-width: 1279px) { body.focus-mode .ticker:not(:empty) { display: flex; } }
@media (max-width: 520px) { .ticker { padding: 6px 16px; } }
/* ---- time-lapse tour ribbon: shown while the page visits every board (the recorder speeds this span up) ---- */
.lapse { position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%); z-index: 45; display: flex; align-items: center; gap: 12px;
  padding: 8px 18px; border-radius: 999px; background: rgba(14, 16, 19, .94); border: 1px solid rgba(245, 185, 66, .75);
  box-shadow: 0 0 26px rgba(245, 185, 66, .25); font-size: 16px; font-weight: 700; letter-spacing: .06em; color: #ffd479; white-space: nowrap; max-width: calc(100vw - 24px); }
.lapse[hidden] { display: none; }
.lapse .ff { font-size: 22px; line-height: 1; letter-spacing: -.12em; animation: lapse-run 1s linear infinite; }
@keyframes lapse-run { 0% { opacity: .35; } 50% { opacity: 1; } 100% { opacity: .35; } }
.lapse .of { font-weight: 600; color: var(--text); letter-spacing: 0; }
.lapse .names { font-weight: 600; color: var(--muted); letter-spacing: 0; overflow: hidden; text-overflow: ellipsis; max-width: 38vw; }
.lapse .bar { width: 90px; height: 5px; border-radius: 3px; background: rgba(255, 255, 255, .14); overflow: hidden; flex: none; }
.lapse .bar i { display: block; height: 100%; background: linear-gradient(90deg, var(--accent), #f5b942); }
@media (max-width: 700px) { .lapse { font-size: 13px; gap: 8px; padding: 6px 12px; } .lapse .names { display: none; } .lapse .bar { width: 50px; } }
@media (prefers-reduced-motion: reduce) { .lapse .ff { animation: none; } }
/* ---- round preview and round results: a full-screen card at the start and end of every round ---- */
.rcard { position: fixed; inset: 0; z-index: 58; display: flex; flex-direction: column; align-items: center; justify-content: safe center; gap: clamp(10px, 2.2vh, 24px);
  padding: clamp(16px, 4vh, 48px) clamp(16px, 4vw, 64px); overflow-y: auto; cursor: pointer;
  background: radial-gradient(ellipse at 50% 0%, rgba(91, 157, 255, .2), transparent 60%), radial-gradient(ellipse at 50% 100%, rgba(245, 185, 66, .14), transparent 55%), rgba(10, 12, 15, .97); }
.rcard.leaving { animation: intro-out .35s ease forwards; }
.rc-kicker { letter-spacing: .32em; text-transform: uppercase; color: var(--accent); font-weight: 700; font-size: clamp(12px, 1.7vh, 17px); text-align: center; animation: rise .5s ease both; }
.rc-head { margin: 0; text-align: center; font-weight: 900; line-height: .95; letter-spacing: -.02em; font-size: clamp(44px, min(11vh, 11vw), 132px); }
.rc-head span { display: inline-block; animation: slam .55s cubic-bezier(.2, 1.5, .4, 1) .15s both; }
.rc-head .of { font-size: .38em; font-weight: 700; color: var(--muted); letter-spacing: 0; margin-left: .3em; animation-delay: .45s; }
.rc-head .gold { background: linear-gradient(180deg, #fff6d2, #f5b942 65%, #c98a1a); -webkit-background-clip: text; background-clip: text; color: transparent; }
.rc-sub { font-size: clamp(14px, 2.2vh, 22px); color: var(--muted); text-align: center; animation: rise .5s ease .6s both; }
.rc-sub b { color: #ffd479; }
.rc-body { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: clamp(14px, 2.4vw, 36px); width: min(1500px, 100%); align-items: start; }
.rc-col h3 { margin: 0 0 8px; font-size: clamp(12px, 1.6vh, 15px); text-transform: uppercase; letter-spacing: .14em; color: var(--muted); }
.rc-m { display: grid; grid-template-columns: 76px minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; gap: 10px; padding: clamp(8px, 1.4vh, 14px) 14px; margin-bottom: 8px;
  border-radius: 12px; background: rgba(23, 26, 31, .94); border: 1px solid var(--line); font-size: clamp(14px, 2.1vh, 22px); font-weight: 650;
  animation: rc-in .5s cubic-bezier(.2, 1.2, .4, 1) both; animation-delay: calc(.8s + var(--i) * .22s); }
@keyframes rc-in { from { opacity: 0; transform: translateX(-40px); } to { opacity: 1; transform: none; } }
.rc-m .bd { white-space: nowrap; color: var(--muted); font-size: .7em; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; }
.rc-m .pl { min-width: 0; overflow-wrap: anywhere; line-height: 1.15; }
.rc-m .pl.b { text-align: right; }
.rc-m .pl small { display: block; font-size: .64em; font-weight: 500; color: var(--muted); margin-top: 2px; }
.rc-m .vs { color: var(--muted); font-size: .75em; font-weight: 700; text-align: center; min-width: 46px; }
.rc-m .vs.res { color: var(--text); font-size: .85em; font-variant-numeric: tabular-nums; }
.rc-m .pl.won { color: #ffd479; }
.rc-m .pl.lost { color: var(--muted); font-weight: 500; }
.rc-m .how { grid-column: 2 / -1; font-size: .62em; font-weight: 500; color: var(--muted); text-align: center; margin-top: -4px; }
.rc-m.star { border-color: rgba(245, 185, 66, .75); background: linear-gradient(90deg, rgba(245, 185, 66, .16), rgba(23, 26, 31, .94) 70%); box-shadow: 0 0 28px rgba(245, 185, 66, .18); }
.rc-m .tag { grid-column: 1 / -1; justify-self: start; font-size: .58em; font-weight: 800; letter-spacing: .16em; text-transform: uppercase; color: #1a1205; background: #f5b942; border-radius: 6px; padding: 1px 8px; margin-bottom: -2px; }
.rc-t { border-radius: 12px; background: rgba(23, 26, 31, .94); border: 1px solid var(--line); padding: 6px 10px; animation: rise .5s ease 1s both; }
.rc-r { display: grid; grid-template-columns: 30px minmax(0, 1fr) 76px 74px; gap: 8px; align-items: center; padding: clamp(3px, .6vh, 6px) 6px; border-radius: 8px;
  font-size: clamp(13px, 1.85vh, 19px); font-variant-numeric: tabular-nums; }
.rc-r.head { color: var(--muted); font-size: clamp(10.5px, 1.3vh, 13px); text-transform: uppercase; letter-spacing: .06em; }
.rc-r .k { color: var(--muted); text-align: center; }
.rc-r .n { min-width: 0; overflow-wrap: anywhere; font-weight: 650; line-height: 1.15; }
.rc-r .p { text-align: right; font-weight: 800; }
.rc-r .wdl { text-align: right; color: var(--muted); }
.rc-r.zone { background: rgba(245, 185, 66, .08); }
.rc-r .p .gain { font-size: .7em; color: var(--ok); font-weight: 700; margin-left: 4px; }
.rc-cut { display: flex; align-items: center; gap: 8px; margin: 3px 0; font-size: clamp(10px, 1.3vh, 12.5px); color: #ffd479; text-transform: uppercase; letter-spacing: .06em; white-space: nowrap; }
.rc-cut::before, .rc-cut::after { content: ""; flex: 1; border-top: 1px dashed rgba(245, 185, 66, .55); }
.rc-next { font-size: clamp(14px, 2.1vh, 21px); text-align: center; animation: rise .5s ease 1.8s both; }
.rc-next b { color: #ffd479; }
.rc-skip { position: absolute; bottom: 14px; right: 18px; font-size: 13px; color: var(--muted); }
.rc-bar { position: absolute; left: 0; bottom: 0; height: 4px; background: linear-gradient(90deg, var(--accent), #f5b942); animation: intro-bar var(--dur, 12s) linear both; }
@media (max-width: 900px) { .rc-body { grid-template-columns: minmax(0, 1fr); } .rc-m { grid-template-columns: 44px minmax(0, 1fr) auto minmax(0, 1fr); } }
@media (prefers-reduced-motion: reduce) { .rcard * { animation-duration: .01s !important; animation-delay: 0s !important; } }
@media (max-width: 900px) {
  .boards > .card.focused { grid-template-columns: minmax(0, 1fr); grid-template-areas: "head" "boardcol" "infocol"; }
  .card.focused .infocol { height: auto; }
  .card.focused .moves { flex: none; max-height: 240px; min-height: 0; }
}
/* ---- hosted page (marvijo.com/ai-chess): one slim line that says how fresh the data is ---- */
.hosted-banner { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 4px 8px; padding: 6px 16px; border-bottom: 1px solid var(--line);
  background: var(--panel); color: var(--muted); font-size: 13px; line-height: 1.35; text-align: center; overflow-wrap: anywhere; }
.hosted-banner .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex: none; }
.hosted-banner .msg { min-width: 0; }
.hosted-banner.live { color: var(--text); }
.hosted-banner.live .dot { background: var(--ok); box-shadow: 0 0 0 3px rgba(52, 199, 123, .18); }
.hosted-banner.off { color: #ffd479; }
.hosted-banner.off .dot { background: var(--warn); }
.hosted-banner.final { color: var(--text); }
.hosted-banner.final .dot { background: #f5b942; }
.hosted-banner.final b { color: #ffd479; }
.hosted-banner .hear { margin-left: 8px; padding: 2px 10px; border-radius: 999px; border: 1px solid rgba(245, 185, 66, .6); color: #ffd479; font-weight: 600; cursor: pointer; }
/* ---- local page: is this tournament published to the hosted page? ---- */
a.chip.pub { text-decoration: none; }
.chip.pub.live { color: var(--ok); border-color: rgba(52, 199, 123, .45); }
.chip.pub.off { color: var(--warn); border-color: rgba(245, 185, 66, .45); }
@media (max-width: 520px) {
  main { padding: 12px 16px; } header { padding: 12px 16px; }
  .boards { grid-template-columns: minmax(0, 1fr); }
  .boards > .card[data-game] { max-width: none; }
  .pbar .think { min-width: 0; }
  td, th { padding: 6px 4px; font-size: 13px; }
}
</style>
</head>
<body>
<header>
  <h1 id="title">AI Chess Swiss</h1>
  <div class="chips" id="chips"></div>
</header>
<div class="ticker" id="ticker" aria-label="Standings"></div>
<main>
  <section class="boards" id="boards"><div class="empty card">Waiting for the tournament to start...</div></section>
  <aside class="side" id="side">
    <div class="card" id="standingsCard"><h2 id="standingsTitle">Standings (Elo)</h2><div id="standings"></div></div>
    <div class="card" id="agentsCard" style="display:none"><h2>Notes and input cache</h2><div id="agents"></div></div>
    <div class="card" id="bracketCard" hidden><h2 id="bracketTitle">Bracket</h2><div id="bracket"></div></div>
    <div class="card"><h2>Rounds</h2><div id="rounds"></div></div>
    <div class="card"><h2>Rules</h2><ul class="rules" id="rules"></ul></div>
  </aside>
</main>
<div class="lapse" id="lapse" hidden role="status" aria-live="off"></div>
<div class="champ-overlay" id="champOverlay" hidden role="dialog" aria-modal="true" aria-labelledby="champName"><canvas id="confetti"></canvas><div class="champ-card" id="champCard"></div></div>
<script>
// Stream mode: ?stream=1, or the server's --stream-layout (it sets window.AICHESS_STREAM before this script).
const STREAM = new URLSearchParams(location.search).get("stream") === "1" || !!window.AICHESS_STREAM;
if (STREAM) document.body.classList.add("stream");
// Hosted mode (marvijo.com/ai-chess): the exporter sets these before this script. The local viewer leaves
// them unset, so every request stays on this server's own /api/... paths.
const API_BASE = window.AICHESS_API_BASE || "";
const HOSTED = !!window.AICHESS_HOSTED;
if (HOSTED) {
  const banner = document.createElement("div");
  banner.id = "hostedBanner";
  banner.className = "hosted-banner wait";
  banner.setAttribute("role", "status");
  banner.innerHTML = `<span class="dot" aria-hidden="true"></span><span class="msg">Connecting to the tournament...</span>`;
  document.body.insertBefore(banner, document.body.firstChild);
}
const GLYPH = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };
const NAG_CLASS = { "??": "blunder", "?": "mistake", "?!": "dubious", "!": "good" };
const NAG_TITLE = { "??": "Blunder", "?": "Mistake", "?!": "Inaccuracy", "!": "Good move (the only good one)" };
let data = null;
let selected = null;          // game id pinned by a click (replay); null = follow live boards
let replayPly = null;         // ply shown for the selected game (null = last)
let focusId = null;           // game shown alone and large (#focus=<id>)
const params = new URLSearchParams(location.search);
function store(key, value) { try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (e) { /* storage blocked */ } return null; }
let analysisOn = store("swissAnalysis") !== "off";
let soundOn = store("swissMoveSound") !== "off";
let commentaryOn = HOSTED ? store("swissCommentary") !== "off" : store("swissCommentary") === "on";   // hosted: on unless the viewer muted it
const replayEval = {};        // "game|ply" -> Stockfish result for replay positions
let replayFetchAt = 0;
const cards = new Map();      // game id -> persistent card DOM + render keys

// Auto-focus: the page follows the board being commentated (or, without commentary, the most
// interesting live board). A board the viewer picks by hand pins the commentator and turns it off.
let autoFocus = !STREAM && store("swissAutoFocus") !== "off";
let focusPinned = false;      // true = the viewer chose this board (pins the commentator); false = auto-focus put it there
let autoReason = "";          // why auto-focus shows the current board (chip text)
let lastAutoSwitch = 0;       // last self-driven switch (never more often than AUTO_GAP_MS)
let lastClipAt = 0;           // last commentary clip start or end: the commentary drives auto-focus while recent
const AUTO_GAP_MS = 20000;
const CLIP_DRIVE_MS = 60000;
const introForced = /(^#|[#&])intro\b/.test(location.hash);
const champForced = /(^#|[#&])champion\b/.test(location.hash);

function readHash() {
  const m = location.hash.match(/focus=([^&]+)/);
  focusId = m ? decodeURIComponent(m[1]) : null;
}
readHash();
if (focusId) { focusPinned = true; autoFocus = false; }   // a #focus link is the viewer's own choice
function setAutoFocus(on) {
  autoFocus = on;
  store("swissAutoFocus", on ? "on" : "off");
  if (on) { focusPinned = false; lastAutoSwitch = 0; autoTick(true); }
  else if (focusId) focusPinned = true;   // stay on this board, now as the viewer's own choice
  render();
}
// opts.auto: auto-focus moved the view (no history entry, commentator not pinned).
function setFocus(id, opts) {
  const auto = !!(opts && opts.auto);
  if (!auto) {
    if (autoFocus) { autoFocus = false; store("swissAutoFocus", "off"); }
    focusPinned = !!id;
  }
  if (id === focusId) { render(); return; }
  focusId = id;
  const url = id ? `#focus=${encodeURIComponent(id)}` : location.pathname + location.search;
  try { if (auto) history.replaceState(null, "", url); else history.pushState(null, "", url); } catch (e) { location.hash = id ? `focus=${encodeURIComponent(id)}` : ""; }
  if (id) window.scrollTo(0, 0);
  render();
  if (auto && id && cards.has(id)) {
    const el = cards.get(id).el;
    el.classList.remove("switch-in"); void el.offsetWidth; el.classList.add("switch-in");
  }
}
function onHistory() {
  const before = focusId;
  readHash();
  if (focusId === before) return;
  if (autoFocus) { autoFocus = false; store("swissAutoFocus", "off"); }   // back/forward is a manual choice
  focusPinned = !!focusId;
  render();
}
window.addEventListener("popstate", onHistory);
window.addEventListener("hashchange", onHistory);

function evalText(a) {
  if (!a) return "...";
  if (a.over) return "game over";
  if (a.mate !== null && a.mate !== undefined) return (a.mate > 0 ? "+M" : "-M") + (a.trackMate ? "" : Math.abs(a.mate));
  if (a.cp === null || a.cp === undefined) return "...";
  return (a.cp > 0 ? "+" : "") + (a.cp / 100).toFixed(2);
}
function whiteShare(a) {
  if (!a || a.over) return 50;
  if (a.mate !== null && a.mate !== undefined) return a.mate > 0 ? 100 : 0;
  if (a.cp === null || a.cp === undefined) return 50;
  return 100 / (1 + Math.exp(-a.cp / 250));
}
let analyzeOff = false;       // /api/analyze said {"enabled": false}: no replay analysis (hosted page)
// data.eval_track[game] = {depth, cp: [White-side score per ply], best: [best move per ply]}: the pusher
// adds it from the viewer's annotations, so the hosted page can show the engine on every position.
function trackEval(game, ply) {
  const t = (data.eval_track || {})[game.id];
  if (!t || !Array.isArray(t.cp)) return undefined;
  const cp = t.cp[ply];
  if (cp === null || cp === undefined) return null;
  const b = Array.isArray(t.best) ? t.best[ply] : null;
  const out = { track: true, engine: data.analysis_engine || data.annotation_engine || "Stockfish", depth: t.depth || 0, best: b || "" };
  if (Math.abs(cp) >= 10000) { out.mate = cp > 0 ? 1 : -1; out.trackMate = true; } else out.cp = cp;
  return out;
}
function evalFor(game, ply, total) {
  if (!analysisOn) return null;
  if (ply >= total && game.status === "live") return (data.analysis || {})[game.id] || null;
  const tracked = trackEval(game, ply);
  if (tracked !== undefined) return tracked;
  if (analyzeOff) return undefined;
  const key = `${game.id}|${ply}`;
  const have = replayEval[key];
  if ((!have || ((have.depth || 0) < 20 && !have.over)) && Date.now() - replayFetchAt > 700) {
    replayFetchAt = Date.now();
    fetch(`${API_BASE}/api/analyze?game=${encodeURIComponent(game.id)}&ply=${ply}`, { cache: "no-store" })
      .then(r => r.ok ? r.json() : null).then(j => {
        if (j && j.enabled === false) { analyzeOff = true; return; }
        if (j && !j.pending) replayEval[key] = j;
      }).catch(() => {});
  }
  return have || null;
}

function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function clock(ms) { ms = Math.max(0, Math.floor((ms || 0) / 1000)); const m = Math.floor(ms / 60), s = ms % 60; return `${m}:${String(s).padStart(2, "0")}`; }
// Write only when the markup changed, so unchanged parts keep their DOM, scroll and hover state.
function setHTML(el, html) { if (el._h !== html) { el.innerHTML = html; el._h = html; } }
function fenBoard(fen) {
  const rows = (fen || "8/8/8/8/8/8/8/8").split(" ")[0].split("/");
  return rows.map(r => { const out = []; for (const ch of r) { if (/\d/.test(ch)) for (let i = 0; i < +ch; i++) out.push(null); else out.push(ch); } return out; });
}
function fenAt(game, ply) {
  // Positions are rebuilt client side from SAN-free UCI moves.
  const moves = game.moves || [];
  let squares = fenBoard("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1");
  let last = null;
  for (let i = 0; i < ply; i++) { squares = applyUci(squares, moves[i].uci); last = moves[i].uci; }
  return { squares, last };
}
function applyUci(sq, uci) {
  const b = sq.map(r => r.slice());
  const f = c => c.charCodeAt(0) - 97, r = c => 8 - (+c);
  const fx = f(uci[0]), fy = r(uci[1]), tx = f(uci[2]), ty = r(uci[3]);
  let pc = b[fy][fx];
  if (!pc) return b;
  if (pc.toLowerCase() === "p" && fx !== tx && !b[ty][tx]) b[fy][tx] = null;          // en passant
  if (pc.toLowerCase() === "k" && Math.abs(tx - fx) === 2) {                         // castling
    if (tx > fx) { b[fy][5] = b[fy][7]; b[fy][7] = null; } else { b[fy][3] = b[fy][0]; b[fy][0] = null; }
  }
  if (uci.length > 4) pc = pc === pc.toUpperCase() ? uci[4].toUpperCase() : uci[4].toLowerCase();
  b[ty][tx] = pc; b[fy][fx] = null;
  return b;
}
function boardHtml(squares, last) {
  const from = last ? last.slice(0, 2) : "", to = last ? last.slice(2, 4) : "";
  let html = "";
  for (let y = 0; y < 8; y++) for (let x = 0; x < 8; x++) {
    const name = String.fromCharCode(97 + x) + (8 - y);
    const pc = squares[y][x];
    const cls = ((x + y) % 2 ? "d" : "l") + (name === from || name === to ? " hl" : "");
    const piece = pc ? `<span class="pc ${pc === pc.toUpperCase() ? "w" : "b"}">${GLYPH[pc.toLowerCase()]}</span>` : "";
    const coordF = y === 7 ? `<span class="coord f">${name[0]}</span>` : "";
    const coordR = x === 0 ? `<span class="coord r">${8 - y}</span>` : "";
    html += `<div class="sq ${cls}">${piece}${coordF}${coordR}</div>`;
  }
  return html;
}
function nagHtml(mark) { return mark ? `<span class="nag ${NAG_CLASS[mark] || ""}" title="${NAG_TITLE[mark] || ""} (Stockfish, viewers only)">${esc(mark)}</span>` : ""; }
function standingRow(name) { return (data.standings || []).find(r => r.name === name) || {}; }
// Stockfish ladder: the player keeps one name ("Stockfish 19"); its depth is shown next to it.
function ladderDepth(name, game) {
  const lad = data.ladder;
  if (!lad || name !== lad.player) return null;
  return game && game.stockfish_depth ? game.stockfish_depth : lad.depth;
}
function ladderTag(name, game) { const d = ladderDepth(name, game); return d ? `<span class="ladder-tag"> \u00b7 depth ${esc(d)}</span>` : ""; }
function renderAgents() {
  const card = document.getElementById("agentsCard");
  if (!card) return;
  const notes = data.latest_notes || {}, cache = data.cache_stats || {};
  const names = (data.players || []).map(p => p.name).filter(n => notes[n] || cache[n]);
  const benched = data.benched || [];
  card.style.display = names.length || benched.length ? "" : "none";
  if (!names.length && !benched.length) return;
  const pctTxt = v => (v === null || v === undefined) ? "" : `${(v * 100).toFixed(1)}%`;
  const when = iso => { const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); };
  const benchRows = benched.map(b => `<div class="ag"><span class="ag-n">${esc(b.name)}</span><span class="ag-c">sits out</span>`
    + `<span class="ag-note" title="${esc(b.reason || "")}">benched: ${esc(b.kind || "preflight failed")}${b.resets_at ? ", resets " + esc(when(b.resets_at)) : ""}</span></div>`).join("");
  setHTML(document.getElementById("agents"), benchRows + names.map(n => {
    const c = cache[n] || {}, note = notes[n];
    const hit = c.warm_hit_rate !== null && c.warm_hit_rate !== undefined ? c.warm_hit_rate : c.hit_rate;
    return `<div class="ag"><span class="ag-n">${esc(n)}</span><span class="ag-c" title="Cached input tokens / all input tokens (without each game's first 3 moves)">${hit !== undefined && hit !== null ? "cache " + pctTxt(hit) : ""}</span>`
      + (note ? `<span class="ag-note" title="${esc(note.game)} ply ${esc(note.ply)}">${esc(note.note)}</span>` : "") + `</div>`;
  }).join(""));
}
function route(name) { const p = (data.players || []).find(x => x.name === name); return p ? (p.route || p.provider) : ""; }

// ---- persistent game cards --------------------------------------------------------------
function cardFor(id) {
  let c = cards.get(id);
  if (c) return c;
  const el = document.createElement("div");
  el.className = "card";
  el.dataset.game = id;
  el.innerHTML = `<div class="game-head" data-focus-head></div>
    <div class="boardcol"><div class="pbar" data-part="black"></div>
      <div class="board-wrap" data-part="wrap"><div class="evalbar" data-part="evalbar" title="Stockfish evaluation, White at the bottom"><div class="white" data-part="evalwhite"></div><div class="mid"></div></div><div class="board" data-part="board"></div></div>
      <div class="pbar" data-part="white"></div><div class="caption" data-part="caption" style="display:none"></div><div class="lb lb-under" data-part="lbunder"></div></div>
    <div class="infocol"><div class="mini-bk" data-part="minibk"></div><div class="evalline" data-part="evalline"></div><div class="comment" data-part="comment"></div>
      <div class="comment" data-part="result" style="display:none"></div><div class="moves" data-part="moves"></div><div class="nav" data-part="nav"></div>
      <div class="thinking" data-part="thinking"><button type="button" class="think-head" data-part="thinkhead" data-think-toggle aria-expanded="false"></button><div class="think-body" data-part="thinkbody" hidden></div></div></div>
    <div class="lb lb-side" data-part="lbside"></div>`;
  const parts = { head: el.querySelector("[data-focus-head]") };
  el.querySelectorAll("[data-part]").forEach(n => { parts[n.dataset.part] = n; });
  c = { id, el, parts, follow: true, top: 0, movesKey: null, boardKey: null, moveTotal: undefined, movePly: undefined, think: newThink(null) };
  parts.moves.addEventListener("scroll", () => {
    const box = parts.moves;
    c.top = box.scrollTop;
    c.follow = box.scrollHeight - box.scrollTop - box.clientHeight < 8;
  }, { passive: true });
  parts.thinkbody.addEventListener("scroll", () => {
    const box = parts.thinkbody;
    if (box.hidden || !box.clientHeight) return;   // hidden or detached: keep the last real position
    c.think.top = box.scrollTop;
    c.think.follow = box.scrollHeight - box.scrollTop - box.clientHeight < 8;
  }, { passive: true });
  cards.set(id, c);
  return c;
}
function updateBar(el, game, side, now) {
  const live = game.status === "live";
  const name = game[side];
  const s = standingRow(name);
  const toMove = (game.fen || "").split(" ")[1] === "b" ? "black" : "white";
  const moving = live && toMove === side;
  const ticking = moving && game.thinking && game.thinking.side === side;
  const think = ticking && game.thinking.since_epoch_ms ? Math.max(0, now - game.thinking.since_epoch_ms) : 0;
  // The side to move's clock runs down live from the moment its model starts thinking.
  let clk = game.clocks ? game.clocks[side] : ((data.config || {}).timeControlMs || 0);
  if (ticking) clk = Math.max(0, clk - think);
  const waiting = live && game.waiting && game.waiting.side === side;
  const thinking = waiting ? "waiting for usage limit" : ticking ? `thinking ${clock(think)}` : (moving ? "starting..." : "");
  el.classList.toggle("to-move", moving);
  setHTML(el, `<span class="dot ${side[0]}"></span><span class="name" title="${esc(route(name))}">${esc(name)}${ladderTag(name, game)}</span>`
    + `<span class="elo">${s.elo ? Math.round(s.elo) : ""}</span><span class="think">${thinking}</span><span class="clock">${clock(clk)}</span>`);
}
function keepVisible(box, el) {
  if (!el) return;
  const top = el.offsetTop, bottom = top + el.offsetHeight;
  if (top < box.scrollTop) box.scrollTop = Math.max(0, top - 6);
  else if (bottom > box.scrollTop + box.clientHeight) box.scrollTop = bottom - box.clientHeight + 6;
}
function updateMoves(c, game, ply, pinned, ann) {
  const moves = game.moves || [];
  const total = moves.length;
  const key = `${game.id}|${total}|${ply}|${pinned ? 1 : 0}|${JSON.stringify(ann)}`;
  if (c.movesKey === key) return;            // nothing changed: leave the list (and its scroll) alone
  c.movesKey = key;
  const box = c.parts.moves;
  const html = moves.map((m, i) => {
    const p = i + 1;
    const num = m.side === "white" ? `<span class="num">${Math.ceil(m.ply / 2)}.</span>` : (i === 0 ? `<span class="num">${Math.ceil(m.ply / 2)}...</span>` : "");
    const cls = "mv" + (p === ply ? " cur" : "") + (m.tries > 1 ? " bad" : "");
    return `${num}<span class="${cls}" data-ply="${p}" title="${m.tries > 1 ? "needed " + m.tries + " tries" : ""}">${esc(m.san)}${nagHtml(ann[p])}</span>`;
  }).join(" ") || "&nbsp;";
  const grew = c.moveTotal === undefined || total > c.moveTotal;
  const plyMoved = c.movePly !== undefined && c.movePly !== ply;
  box.innerHTML = html;
  box._h = html;
  if (pinned && plyMoved) keepVisible(box, box.querySelector(".cur"));
  else if (c.follow && (grew || plyMoved)) box.scrollTop = box.scrollHeight;   // following the newest move
  else box.scrollTop = c.top;                                                   // user scrolled away: hold still
  c.top = box.scrollTop;
  c.moveTotal = total;
  c.movePly = ply;
}
// ---- thinking panel: the text a model streams while it decides a move -------------------------
const THINK_KEEP = 150000;    // characters kept per panel; older text is dropped from the top
const thinkOpen = { grid: store("swissThinking.grid") === "open", focus: store("swissThinking.focus") !== "closed" };
function thinkMode() { return focusId ? "focus" : "grid"; }
function newThink(key) { return { key, ply: 0, live: false, name: "", text: "", since: 0, size: 0, exists: null, clipped: false, loaded: false, inflight: false, follow: true, top: 0 }; }
function sizeText(n) { return n < 1000 ? `${n} chars` : `${(n / 1000).toFixed(1)}k chars`; }
function thinkTarget(game, ply, total) {
  // Following a live game: the side to move is thinking about ply total + 1 right now.
  if (game.status === "live" && ply >= total) return { ply: total + 1, live: true };
  return { ply, live: false };   // the thinking that produced move `ply` (0 = start position)
}
function drawThinkHead(c) {
  const t = c.think;
  const meta = (t.live ? `<span class="think-dot"></span><span class="live">thinking...</span>` : "") + (t.exists ? `<span>${sizeText(t.size)}</span>` : "");
  setHTML(c.parts.thinkhead, `<span class="chev"></span><span class="label">Thinking${t.name ? " - " + esc(t.name) : ""}</span><span class="meta">${meta}</span>`);
}
function restoreThinkScroll(c) {
  const box = c.parts.thinkbody, t = c.think;
  if (box.hidden) return;
  box.scrollTop = t.live && t.follow ? box.scrollHeight : t.top;
}
// chunk: text just appended to t.text (append only), or null to redraw the whole panel.
function paintThinking(c, chunk) {
  const t = c.think, box = c.parts.thinkbody;
  let note = "";
  if (t.ply < 1) note = "Start position: pick a move to see the thinking behind it.";
  else if (!t.loaded) note = "Loading...";
  else if (!t.exists) note = "No visible thinking for this move.";
  else if (!t.text) note = "Waiting for the first words...";
  if (note) {
    if (box._sig !== note) {
      const d = document.createElement("div");
      d.className = "think-note"; d.textContent = note;
      box.replaceChildren(d); box._sig = note; box._text = null;
      t.top = 0; box.scrollTop = 0;
    }
    return;
  }
  if (chunk !== null && box._sig === "text" && box._text) {
    if (!chunk) return;
    box._text.appendData(chunk);
    if (t.live && t.follow) box.scrollTop = box.scrollHeight;   // at the bottom: follow the new words
    return;                                                     // scrolled up: appending leaves the view still
  }
  const nodes = [];
  if (t.clipped) {
    const s = document.createElement("span");
    s.className = "think-clip"; s.textContent = "Older text hidden - showing the newest part.";
    nodes.push(s);
  }
  box._text = document.createTextNode(t.text);
  nodes.push(box._text);
  const first = box._sig !== "text";
  box.replaceChildren(...nodes); box._sig = "text";
  if (t.live) box.scrollTop = t.follow ? box.scrollHeight : t.top;
  else if (first) { box.scrollTop = 0; t.top = 0; }              // replay: read it from the top
  else box.scrollTop = t.top;
}
async function fetchThinking(c) {
  const t = c.think;
  if (t.inflight || t.ply < 1) return;
  t.inflight = true;
  const q = params.get("id") ? `&id=${encodeURIComponent(params.get("id"))}` : "";
  try {
    const res = await fetch(`${API_BASE}/api/thinking?game=${encodeURIComponent(c.id)}&ply=${t.ply}&since=${t.since}${q}`, { cache: "no-store" });
    const j = res.ok ? await res.json() : null;
    if (!j || c.think !== t) return;   // failed (retried next tick) or already on another move
    const firstLoad = !t.loaded;
    t.loaded = true;
    t.exists = !!j.exists;
    let chunk = null;
    if (!j.exists) { t.text = ""; t.since = 0; t.size = 0; t.clipped = false; }
    else {
      if (!firstLoad && t.since > 0 && j.from === t.since && !j.truncated) { chunk = j.text; t.text += j.text; }
      else { t.text = j.text; t.clipped = !!j.truncated || j.from > 0; }
      t.size = j.size; t.since = j.size;
      if (t.text.length > THINK_KEEP) { t.text = t.text.slice(-Math.floor(THINK_KEEP * 0.8)); t.clipped = true; chunk = null; }
    }
    if (firstLoad || chunk !== "") paintThinking(c, firstLoad ? null : chunk);
    drawThinkHead(c);
  } catch (e) { /* try again next tick */ } finally { t.inflight = false; }
}
function updateThinking(c, game, ply, total) {
  const p = c.parts;
  const tg = thinkTarget(game, ply, total);
  const key = `${game.id}|${tg.ply}`;
  if (c.think.key !== key) {
    c.think = newThink(key);
    c.think.ply = tg.ply;
    c.think.name = tg.ply > 0 ? game[tg.ply % 2 ? "white" : "black"] : "";
    p.thinkbody._sig = null;
  }
  const t = c.think;
  t.live = tg.live;
  drawThinkHead(c);
  const open = thinkOpen[thinkMode()];
  p.thinking.classList.toggle("open", open);
  if (p.thinkhead.getAttribute("aria-expanded") !== String(open)) p.thinkhead.setAttribute("aria-expanded", String(open));
  if (!open) { if (!p.thinkbody.hidden) p.thinkbody.hidden = true; return; }
  const opened = p.thinkbody.hidden;
  if (opened) p.thinkbody.hidden = false;
  if (p.thinkbody._sig === null || p.thinkbody._sig === undefined) paintThinking(c, null);
  else if (opened) restoreThinkScroll(c);
  if (!t.loaded) fetchThinking(c);
}
setInterval(() => {
  if (!thinkOpen[thinkMode()]) return;
  for (const c of cards.values()) {
    const t = c.think;
    if (c.el.isConnected && t.ply >= 1 && (t.live || !t.loaded)) fetchThinking(c);
  }
}, HOSTED ? 2000 : 1000);

function updateCard(c, game, now) {
  const p = c.parts;
  const live = game.status === "live";
  const pinned = selected === game.id;
  const focused = focusId === game.id;
  const moves = game.moves || [];
  const total = moves.length;
  const ply = pinned && replayPly !== null ? Math.min(replayPly, total) : total;
  const ann = (data.annotations || {})[game.id] || {};
  c.el.classList.toggle("focused", focused);
  const onAir = (!focusId || !focusPinned) && clipPlaying && caption && caption.game === game.id;
  c.el.classList.toggle("on-air", !!onAir);
  const pill = live ? `<span class="result-pill live">LIVE - move ${Math.floor(total / 2) + 1}</span>`
    : `<span class="result-pill">${esc(game.status === "pending" ? "next" : (game.result || "*"))}</span>`;
  const btn = focused ? `<button class="linkbtn small" data-unfocus title="Back to all boards (Esc)">Back to all boards</button>`
    : `<button class="linkbtn small" data-focus="${esc(game.id)}" title="Show only this board, large">Focus</button>`;
  const hit = pairingOf(game.id);
  const pr = hit ? hit.pairing : null;
  const label = (pr && pr.label) || game.label || `Round ${game.round} - Board ${game.board}`;
  const koGame = !!((pr && pr.match) || game.match);
  const cfg = data.config || {};
  const armaInc = cfg.armageddonIncrementMs ?? cfg.incrementMs ?? 0;
  const arma = isArmageddon(game, pr) ? `<span class="arma-band" title="Armageddon decider: White has more time, a draw counts as a Black win"><b>ARMAGEDDON</b><span class="odds">draw = Black wins</span>`
    + `<span class="clk">White ${clock(cfg.armageddonWhiteMs || 600000)}, Black ${clock(cfg.armageddonBlackMs || 450000)}${armaInc ? `, +${Math.round(armaInc / 1000)} s` : ""}</span></span>` : "";
  setHTML(p.head, `<span class="tag ${koGame ? "ko" : ""}">${esc(label)}${pinned && ply < total ? " - replay" : ""}</span><span class="head-right">${onAir ? `<span class="air-tag" title="The spoken commentary is about this board">On commentary</span>` : ""}${pill}${btn}</span>${arma}`);
  updateBar(p.black, game, "black", now);
  updateBar(p.white, game, "white", now);
  setHTML(p.minibk, focused ? miniBracketHtml(game.id) : "");
  const lb = focused ? leaderboardHtml(game) : "";   // CSS shows it beside the board (wide) or under it
  setHTML(p.lbside, lb);
  setHTML(p.lbunder, lb);
  const boardKey = `${total}|${ply}|${game.fen}`;
  if (c.boardKey !== boardKey) {
    c.boardKey = boardKey;
    const pos = ply < total ? fenAt(game, ply) : { squares: fenBoard(game.fen), last: total ? moves[total - 1].uci : null };
    p.board.innerHTML = boardHtml(pos.squares, pos.last);
  }
  // undefined = analysis panel hidden; null = waiting for the first result.
  const ev = analysisOn && data.analysis_engine ? evalFor(game, ply, total) : undefined;
  p.wrap.classList.toggle("no-eval", ev === undefined);
  p.evalbar.style.display = ev === undefined ? "none" : "";
  p.evalline.style.display = ev === undefined ? "none" : "";
  if (ev !== undefined) {
    const h = whiteShare(ev).toFixed(1) + "%";
    if (p.evalwhite.style.height !== h) p.evalwhite.style.height = h;
    setHTML(p.evalline, `<span class="score">${evalText(ev)}</span><span class="pv">${ev && ev.best ? "best " + esc(ev.best) + (ev.pv ? " - " + esc(ev.pv) : "") : (ev && (ev.over || ev.track) ? "" : "analysing...")}</span><span class="eng">${esc((ev && ev.engine) || data.analysis_engine || "Stockfish")}${ev && ev.depth && ev.depth < 99 ? " d" + ev.depth : ""}</span>`);
  }
  const shownMove = ply > 0 ? moves[ply - 1] : null;
  const commentWho = shownMove ? `${esc(game[shownMove.side])} - ${Math.ceil(shownMove.ply / 2)}${shownMove.side === "white" ? "." : "..."} ${esc(shownMove.san)}${nagHtml(ann[ply])}`
    + (shownMove.tries > 1 ? ` (try ${shownMove.tries})` : "") + ` - ${((shownMove.elapsed_ms || 0) / 1000).toFixed(0)}s`
    + (shownMove.hurried ? ` - thinking stopped at the cap` : "") : "No moves yet";
  setHTML(p.comment, `<div class="who">${commentWho}</div>${esc(shownMove ? shownMove.comment : "")}`);
  p.result.style.display = game.termination ? "" : "none";
  setHTML(p.result, game.termination ? `<div class="who">Result</div>${esc(game.termination)}` : "");
  const cap = caption && caption.game === game.id ? caption.text : "";
  p.caption.style.display = cap ? "" : "none";
  setHTML(p.caption, (cap && caption.tag ? `<span class="cap-on">${esc(caption.tag)}</span>` : "") + esc(cap));
  updateMoves(c, game, ply, pinned, ann);
  updateThinking(c, game, ply, total);
  setHTML(p.nav, `<button data-nav="first" title="First position">|&lt;</button><button data-nav="prev" title="Previous move (Left arrow)">&lt;</button><button data-nav="next" title="Next move (Right arrow)">&gt;</button><button data-nav="last" title="Last move">&gt;|</button>`
    + (pinned ? `<button data-nav="close">Back to live</button>` : "") + `<span class="ply">ply ${ply} / ${total}</span>`);
}
function shownGames() {
  const games = data.games || {};
  if (focusId) return games[focusId] ? [games[focusId]] : [];
  const rnd = (data.rounds || []).find(r => r.round === data.current_round);
  let shown = rnd ? rnd.pairings.map(p => games[p.game_id]).filter(Boolean) : [];
  if (selected && games[selected] && !shown.some(g => g.id === selected)) shown = [games[selected]].concat(shown.filter(g => g.status === "live"));
  return shown;
}
function renderBoards() {
  const boards = document.getElementById("boards");
  document.body.classList.toggle("focus-mode", !!focusId);
  const shown = shownGames();
  const sig = shown.map(g => g.id).join(",") + "|" + (focusId || "");
  if (boards._sig !== sig) {
    boards._sig = sig;
    let nodes;
    if (shown.length) {
      nodes = shown.map(g => cardFor(g.id).el);
    } else {
      const empty = document.createElement("div");
      empty.className = "empty card";
      empty.innerHTML = focusId ? `Game ${esc(focusId)} is not in this tournament yet. <button class="linkbtn" data-unfocus>Back to all boards</button>` : "Waiting for pairings...";
      nodes = [empty];
    }
    boards.replaceChildren(...nodes);
    // Re-attached cards lose their scroll offset: redraw their move lists once.
    for (const g of shown) { const c = cardFor(g.id); c.movesKey = null; c.moveTotal = undefined; restoreThinkScroll(c); }
  }
  // Hosted and offline: the clocks stop at the last update instead of running down to 0:00.
  const age = HOSTED ? stateAgeS() : null;
  const now = typeof age === "number" && age >= 90 && data.updated_epoch_ms ? Number(data.updated_epoch_ms) : Date.now();
  for (const g of shown) updateCard(cardFor(g.id), g, now);
}

// Stream mode: if the side column or the boards grow taller than the screen (bracket, long notes), shrink
// them to fit instead of cutting anything off (Chrome's zoom keeps layout and hit boxes consistent).
function fitStream() {
  for (const el of [document.getElementById("side"), document.getElementById("boards")]) {
    if (!el) continue;
    el.style.zoom = "";
    const room = el.clientHeight, need = el.scrollHeight;
    if (room > 0 && need > room + 1) el.style.zoom = Math.max(0.5, Math.floor(room / need * 1000) / 1000);
  }
}
function render() {
  if (STREAM) requestAnimationFrame(fitStream);
  if (!data || !data.id) return;
  document.getElementById("title").textContent = data.title || "AI Chess Swiss";
  document.title = (focusId && data.games && data.games[focusId] ? `${data.games[focusId].white} vs ${data.games[focusId].black} - ` : "") + (data.title || "AI Chess Swiss");
  const cfg = data.config || {};
  const finished = data.finished;
  const fmt = fmtInfo();
  const koSize = (fmt && fmt.ko_size) || cfg.knockoutSize || 4;
  setHTML(document.getElementById("chips"), [
    stageChip(cfg),
    data.paused ? `<span class="chip" title="${esc(data.paused)}">Paused: a provider is unavailable; the game will be replayed</span>` : "",
    `<span class="chip"><b>${Math.round((cfg.timeControlMs || 0) / 60000)} min${cfg.incrementMs ? " + " + Math.round(cfg.incrementMs / 1000) + " s" : ""}</b> per player</span>`,
    `<span class="chip"><b>${cfg.maxAttempts}</b> tries per move, then forfeit</span>`,
    fmt ? `<span class="chip">Round robin, top <b>${koSize}</b> to the knockouts, Elo start <b>${cfg.startElo}</b></span>`
      : `<span class="chip">Swiss, Elo start <b>${cfg.startElo}</b>, K=${cfg.eloK}</span>`,
    `<span class="chip btn ${autoFocus ? "on" : ""}" data-toggle-autofocus title="${autoFocus ? "The page follows the commentary, or the most interesting live board. Click a board yourself to stay on it." : "Click to follow the commentary, or the most interesting live board, automatically"}">Auto-focus: <b>${autoFocus ? "on" : "off"}</b>${autoFocus && focusId && autoReason ? ` - ${esc(autoReason)}` : ""}</span>`,
    data.analysis_engine ? `<span class="chip btn ${analysisOn ? "on" : ""}" data-toggle-analysis title="Viewer-only engine analysis; the AI players never see it">${esc(data.analysis_engine)} analysis: <b>${analysisOn ? "on" : "off"}</b></span>` : "",
    `<span class="chip btn ${soundOn ? "on" : ""}" data-toggle-sound title="A short click whenever a new move appears on a visible board">Move sound: <b>${soundOn ? "on" : "off"}</b></span>`,
    data.commentary ? `<span class="chip btn ${commentaryOn ? (needGesture ? "wait" : "on") : ""}" data-toggle-commentary title="Spoken commentary: it moves between the leaders and the most interesting games; in focus mode it stays on the focused board">Commentary: <b>${commentaryOn ? (needGesture ? "click to start" : "on") : "muted"}</b></span>` : "",
    publishChip(),
  ].join(""));
  renderBanner();
  renderBoards();
  const games = data.games || {};
  document.getElementById("standingsTitle").textContent = fmt ? "Round robin table (Elo)" : "Standings (Elo)";
  renderTicker();
  renderBracket();
  const rows = (data.standings || []).map(r => {
    const d = r.elo_delta || 0;
    return `<tr class="${r.rank === 1 && (r.played || 0) > 0 ? "rank1" : ""}"><td>${r.rank}</td><td class="player">${esc(r.name)}${ladderTag(r.name)}<span class="route">${esc(route(r.name))}</span></td>`
      + `<td class="num"><b>${r.points}</b></td><td class="num">${Math.round(r.elo)} <span class="${d > 0 ? "up" : d < 0 ? "down" : ""}">${Math.round(d) ? (d > 0 ? "+" : "") + Math.round(d) : ""}</span></td>`
      + `<td class="num">${r.wins}/${r.draws}/${r.losses}</td><td class="num">${r.forfeits}</td><td class="num">${r.flags}</td><td class="num">${r.invalid_attempts}</td></tr>`;
  }).join("");
  renderAgents();
  setHTML(document.getElementById("standings"), `<table><thead><tr><th>#</th><th>Player</th><th class="num">Pts</th><th class="num">Elo</th><th class="num">W/D/L</th><th class="num" title="Lost by 3 invalid replies">Forf</th><th class="num" title="Lost on time">Flag</th><th class="num" title="Rejected replies">Bad</th></tr></thead><tbody>${rows}</tbody></table>`);
  setHTML(document.getElementById("rounds"), (data.rounds || []).slice().reverse().map(r => {
    const pairs = r.pairings.map(p => {
      const g = games[p.game_id] || {};
      const res = g.status === "live" ? `<span class="r live">LIVE</span>` : `<span class="r">${esc(g.result && g.result !== "*" ? g.result.replace("1/2-1/2", "½-½") : "-")}</span>`;
      const lbl = p.label ? `<span class="lbl">${esc(p.label)}${p.armageddon ? `<span class="a">ARMAGEDDON</span>` : ""}</span>` : "";
      return `<div class="pair ${selected === p.game_id ? "sel" : ""}" data-pick="${esc(p.game_id)}">${lbl}<span class="w">${esc(p.white)}</span>${res}<span class="b">${esc(p.black)}</span>`
        + (g.termination ? `<span class="why">${esc(g.termination)}</span>` : "") + `</div>`;
    }).join("");
    const title = r.label || (fmt && !r.stage ? `Round ${r.round} of ${fmt.rr_rounds || cfg.rounds}` : `Round ${r.round}`);
    return `<div class="round"><div class="round-title"><span>${esc(title)}</span><span>${r.status === "finished" ? "done" : "playing"}</span></div>${pairs}${r.bye ? `<div class="bye">Bye: ${esc(r.bye)} (sits out this round${cfg.byePoints ? `, +${cfg.byePoints}` : ", no points"})</div>` : ""}</div>`;
  }).join("") || `<div class="empty">No rounds yet</div>`);
  const formatRules = fmt ? [
    `Round robin, everyone plays everyone once; top ${koSize} to the knockouts.`,
    "Points: 1 for a win, 0.5 for a draw, 0 for a loss. The round-robin table seeds the knockouts.",
    `Knockouts: semifinals 1 v ${koSize} and 2 v ${koSize - 1}, then the Final and a third-place match.`,
    `Knockout draw: an Armageddon decider with colours swapped; White ${clock(cfg.armageddonWhiteMs || 600000)}, Black ${clock(cfg.armageddonBlackMs || 450000)}, a draw counts as a Black win.`,
  ] : [
    "Points: 1 for a win, 0.5 for a draw, 0 for a loss or a bye.",
    "Swiss pairing: same score meets same score, no rematches, one bye each.",
  ];
  setHTML(document.getElementById("rules"), [
    "Each AI picks every move itself: no tools, no code, no chess engine.",
    `${cfg.maxAttempts} replies per move; an illegal or broken reply is rejected with the reason, the third one forfeits the game.`,
    `${Math.round((cfg.timeControlMs || 0) / 60000)} minutes of model thinking time per player, ${cfg.incrementMs ? "+" + Math.round(cfg.incrementMs / 1000) + " s per move" : "no increment"}; the clock runs out = loss on time.`,
    "Every model thinks at High effort. Past the move cap (1.5x its time budget) its thinking stops, it gets all of that thinking back and gives its move. Running out of time is never an invalid reply.",
    ...formatRules,
    "Stockfish analysis is for viewers only: the AI players never see it.",
    data.annotation_engine ? "Move marks (?? ? ?! !) come from Stockfish 19 for viewers only." : "",
    "Thinking shows what each model chose to reveal while deciding: raw reasoning for most API models, summaries for GPT and Claude, search lines for Stockfish.",
  ].filter(Boolean).map(x => `<li>${esc(x)}</li>`).join(""));
  syncCommentaryTarget();
}

// ---- round robin + knockouts (state fields "format", "stage", "knockout"; all optional) -----
function fmtInfo() { return data && data.format && typeof data.format === "object" ? data.format : null; }
function ko() { return data && data.knockout && typeof data.knockout === "object" ? data.knockout : null; }
function stageOf() { return data.stage || (data.finished ? "finished" : (fmtInfo() ? "round-robin" : "")); }
function pairingOf(gid) {
  for (const r of data.rounds || []) for (const p of r.pairings || []) if (p.game_id === gid) return { round: r, pairing: p };
  return null;
}
function isArmageddon(game, pairing) {
  if (!pairing && game && game.id) { const hit = pairingOf(game.id); pairing = hit ? hit.pairing : null; }
  return !!((game && game.armageddon) || (pairing && pairing.armageddon));
}
function crownSvg(cls, uid, dim) {
  const g = `crown-${uid}`;
  return `<svg class="${cls}" viewBox="0 0 64 54" aria-hidden="true"${dim ? ` opacity=".35"` : ""}><defs><linearGradient id="${g}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff3c4"/><stop offset=".55" stop-color="#f5b942"/><stop offset="1" stop-color="#b9780f"/></linearGradient></defs>`
    + `<path d="M6 19 L19 31 L32 9 L45 31 L58 19 L53 43 H11 Z" fill="url(#${g})" stroke="#8a5a06" stroke-width="1.6" stroke-linejoin="round"/>`
    + `<rect x="10" y="44" width="44" height="8" rx="2.5" fill="url(#${g})" stroke="#8a5a06" stroke-width="1.6"/>`
    + `<circle cx="6" cy="17" r="4.2" fill="url(#${g})" stroke="#8a5a06" stroke-width="1.4"/><circle cx="32" cy="7" r="4.6" fill="url(#${g})" stroke="#8a5a06" stroke-width="1.4"/><circle cx="58" cy="17" r="4.2" fill="url(#${g})" stroke="#8a5a06" stroke-width="1.4"/>`
    + `<circle cx="22" cy="48" r="2.3" fill="#ff5d5d"/><circle cx="32" cy="48" r="2.3" fill="#5b9dff"/><circle cx="42" cy="48" r="2.3" fill="#34c77b"/></svg>`;
}
function stageChip(cfg) {
  const k = ko(), st = stageOf(), f = fmtInfo();
  if (k && k.champion) return `<span class="chip stage ko champ btn" data-show-champion title="Show the champion moment again">${crownSvg("ico", "chip")}Champion: <b>${esc(k.champion)}</b></span>`;
  if (f) {
    const rnd = (data.rounds || []).find(r => r.round === data.current_round);
    const armaLive = rnd && (rnd.pairings || []).some(p => p.armageddon && ((data.games || {})[p.game_id] || {}).status === "live");
    if (st === "semifinals" || st === "final") return `<span class="chip stage ko">${st === "final" ? "Final" : "Semifinals"}${armaLive ? " - Armageddon live" : ""}</span>`;
    if (st === "finished" || data.finished) return `<span class="chip done">Finished${data.winner ? ` - winner <b>${esc(data.winner)}</b>` : ""}</span>`;
    return `<span class="chip stage">Round robin - round <b>${data.current_round}</b> of ${f.rr_rounds || cfg.rounds}</span>`;
  }
  return data.finished ? `<span class="chip done">Finished - winner <b>${esc(data.winner || "")}</b></span>` : `<span class="chip live">Round <b>${data.current_round}</b> of ${cfg.rounds}</span>`;
}
function matchGameList(m) {
  const ids = Array.isArray(m.games) ? m.games.slice() : [];
  for (const r of data.rounds || []) for (const p of r.pairings || []) if (p.match === m.id && !ids.includes(p.game_id)) ids.push(p.game_id);
  return ids.map(id => (data.games || {})[id]).filter(Boolean);
}
function seedOf(name) { const k = ko(); const s = k && (k.seeds || []).find(x => x.name === name); return s ? s.seed : ""; }
function loserOf(m) { return m && m.winner ? (m.winner === m.a ? m.b : m.a) : null; }
function bkRow(name, seed, tbd, m, games) {
  if (!name) return `<div class="bk-p"><span class="bk-seed">${seed || "-"}</span><span class="bk-name tbd">${esc(tbd)}</span><span class="bk-res"></span></div>`;
  const win = m && m.winner === name, out = m && m.winner && m.winner !== name;
  const res = games.filter(g => g.white === name || g.black === name).map(g => {
    const colour = g.white === name ? "White" : "Black";
    if (g.status === "live") return `<span class="lv" title="Playing now with ${colour}">${colour[0]}</span>`;
    if (!g.result || g.result === "*") return "";
    const sc = g.result === "1/2-1/2" ? "½" : ((g.result === "1-0") === (g.white === name) ? "1" : "0");
    return `<span class="${isArmageddon(g) ? "a" : ""}" title="${isArmageddon(g) ? "Armageddon decider" : "Game"} with ${colour}">${sc}</span>`;
  }).join("");
  return `<div class="bk-p ${win ? "win" : ""} ${out ? "out" : ""}"><span class="bk-seed" title="Seed">${seed || "-"}</span><span class="bk-name">${esc(name)}</span><span class="bk-res">${res}</span></div>`;
}
// o: { label, a, b, aSeed, bSeed, aTbd, bTbd, preview, cls }
function bkMatchHtml(m, o) {
  const games = m ? matchGameList(m) : [];
  const live = games.find(g => g.status === "live");
  const armaLive = live && isArmageddon(live);
  const arma = games.find(g => isArmageddon(g));
  let state = "", why = "";
  if (m && m.winner) {
    state = `<span class="bk-state won">${m.decided_by === "armageddon" ? "Won in Armageddon" : (m.stage === "semifinals" ? "Through" : "Won")}</span>`;
    if (arma && m.decided_by === "armageddon") why = arma.result === "1/2-1/2" ? `Armageddon drawn: ${m.winner} goes through on Black's draw odds` : `${m.winner} won the Armageddon decider`;
  } else if (armaLive) { state = `<span class="bk-state arma">Armageddon live</span>`; why = "Game 1 drawn: colours swapped, draw = Black wins"; }
  else if (live) state = `<span class="bk-state live">Live</span>`;
  else if (o.preview) state = `<span class="bk-state">Projected</span>`;
  else state = `<span class="bk-state">${games.length ? "Decider next" : "Up next"}</span>`;
  const shown = live || games.filter(g => (g.moves || []).length || g.status === "finished").slice(-1)[0];
  const cls = ["bk-match", o.cls || "", live ? "live" : "", armaLive ? "arma" : "", m && m.winner ? "done" : "", shown && !o.preview ? "click" : ""].filter(Boolean).join(" ");
  const attrs = shown && !o.preview ? ` data-bk-game="${esc(shown.id)}" title="${live ? "Watch this game" : "Show this game"}"` : "";
  return `<div class="${cls}"${attrs}><div class="bk-head"><span class="t">${esc(o.label)}</span>${state}</div>`
    + bkRow(o.a, o.aSeed, o.aTbd, m, games) + bkRow(o.b, o.bSeed, o.bTbd, m, games)
    + (why ? `<div class="bk-why">${esc(why)}</div>` : "") + `</div>`;
}
// The bracket as data: the side panel and the compact focus-mode bracket draw the same slots.
function bracketSlots() {
  const f = fmtInfo(), k = ko();
  if (!(k || (f && String(f.type || "").includes("knockout")))) return null;
  const out = { ko: !!k, champion: k && k.champion ? k.champion : null };
  if (k) {
    const ms = k.matches || [];
    const semis = ms.filter(m => m.stage === "semifinals");
    const sf1 = ms.find(m => m.id === "sf1") || semis[0] || null, sf2 = ms.find(m => m.id === "sf2") || semis[1] || null;
    const fin = ms.find(m => m.id === "final") || null, third = ms.find(m => m.id === "third") || null;
    const semi = (m, n) => ({ m, o: { label: (m && m.label) || `Semifinal ${n}`, a: m && m.a, b: m && m.b, aSeed: m && seedOf(m.a), bSeed: m && seedOf(m.b), aTbd: "To be decided", bTbd: "To be decided" } });
    const fa = fin ? fin.a : sf1 && sf1.winner, fb = fin ? fin.b : sf2 && sf2.winner;
    const ta = third ? third.a : loserOf(sf1), tb = third ? third.b : loserOf(sf2);
    out.sf1 = semi(sf1, 1); out.sf2 = semi(sf2, 2);
    out.fin = { m: fin, o: { label: "Final", cls: "bk-final", a: fa, b: fb, aSeed: seedOf(fa), bSeed: seedOf(fb), aTbd: "Winner of Semifinal 1", bTbd: "Winner of Semifinal 2" } };
    out.third = { m: third, o: { label: "Third place", a: ta, b: tb, aSeed: seedOf(ta), bSeed: seedOf(tb), aTbd: "Loser of Semifinal 1", bTbd: "Loser of Semifinal 2" } };
  } else {
    const cfg = data.config || {};
    const size = f.ko_size || 4, rr = f.rr_rounds || cfg.rounds;
    const rows = (data.standings || []).slice().sort((a, b) => (a.rank || 99) - (b.rank || 99));
    const played = rows.some(r => (r.played || 0) > 0);
    const done = (data.rounds || []).filter(r => !r.stage && r.status === "finished").length;
    const nm = i => (played && rows[i] ? rows[i].name : null);
    out.note = played ? `Projected only: who would meet if the round robin ended now (after ${done} of ${rr} rounds). It changes after every game; the real semifinals (1 v ${size}, 2 v ${size - 1}) start after round ${rr}.`
      : `The top ${size} of the round robin meet here: 1 v ${size} and 2 v ${size - 1}.`;
    out.short = played ? `Projected if it ended now (after round ${done} of ${rr})` : `Top ${size} of the round robin`;
    out.sf1 = { m: null, o: { label: "Semifinal 1", preview: true, a: nm(0), b: nm(size - 1), aSeed: 1, bSeed: size, aTbd: "Seed 1", bTbd: `Seed ${size}` } };
    out.sf2 = { m: null, o: { label: "Semifinal 2", preview: true, a: nm(1), b: nm(size - 2), aSeed: 2, bSeed: size - 1, aTbd: "Seed 2", bTbd: `Seed ${size - 1}` } };
    out.fin = { m: null, o: { label: "Final", preview: true, cls: "bk-final", aTbd: "Winner of Semifinal 1", bTbd: "Winner of Semifinal 2" } };
    out.third = null;
  }
  return out;
}
function renderBracket() {
  const card = document.getElementById("bracketCard");
  const sl = bracketSlots();
  if (card.hidden === !!sl) card.hidden = !sl;
  document.getElementById("side").classList.toggle("ko-first", !!(sl && sl.ko));
  if (!sl) return;
  document.getElementById("bracketTitle").textContent = sl.ko ? "Knockout bracket" : "Road to the final (projected)";
  const champ = sl.champion ? `<div class="bk-champ won">${crownSvg("", "bk")}<span><span class="bk-head" style="justify-content:center;margin:0">Champion</span><b>${esc(sl.champion)}</b></span></div>`
    : `<div class="bk-champ">${crownSvg("", "bk", true)}<span>Champion: to be crowned</span></div>`;
  const html = (sl.note ? `<div class="bk-note">${esc(sl.note)}</div>` : "")
    + `<div class="bk-semis">${bkMatchHtml(sl.sf1.m, sl.sf1.o)}${bkMatchHtml(sl.sf2.m, sl.sf2.o)}</div><div class="bk-join"></div>`
    + bkMatchHtml(sl.fin.m, sl.fin.o) + champ
    + (sl.third ? `<div class="bk-third">${bkMatchHtml(sl.third.m, sl.third.o)}</div>` : "");
  setHTML(document.getElementById("bracket"), html);
}
// Compact bracket beside the big board (focus mode, wide screens only; CSS hides it elsewhere).
function miniMatch(m, o, curId) {
  const games = m ? matchGameList(m) : [];
  const live = games.find(g => g.status === "live");
  const armaLive = live && isArmageddon(live);
  const arma = games.some(g => isArmageddon(g));
  let state;
  if (m && m.winner) state = `<span class="won">${m.decided_by === "armageddon" ? "Won in Armageddon" : (m.stage === "semifinals" ? "Through" : "Won")}</span>`;
  else if (armaLive) state = `<span class="arma">Armageddon live</span>`;
  else if (live) state = `<span class="live">Live</span>`;
  else if (arma) state = `<span class="arma">Armageddon next</span>`;
  else state = `<span>${o.preview ? "Projected" : (games.length ? "Decider next" : "Up next")}</span>`;
  const shown = live || games.filter(g => (g.moves || []).length || g.status === "finished").slice(-1)[0];
  const here = !!curId && games.some(g => g.id === curId);
  const click = shown && !o.preview && shown.id !== curId;
  const row = (name, seed, tbd) => {
    tbd = String(tbd || "").replace(/^(Winner|Loser) of Semifinal (\d)$/, (x, w, n) => `${w}, semi ${n}`);
    if (!name) return `<div class="mb-p"><span class="bk-seed">${seed || "-"}</span><span class="mb-n tbd">${esc(tbd)}</span></div>`;
    const win = m && m.winner === name, out = m && m.winner && m.winner !== name;
    return `<div class="mb-p ${win ? "win" : ""} ${out ? "out" : ""}"><span class="bk-seed">${seed || "-"}</span><span class="mb-n">${esc(name)}</span></div>`;
  };
  const cls = ["mb-m", o.label === "Third place" ? "third" : "", live ? "live" : "", armaLive ? "arma" : "", m && m.winner ? "done" : "", here ? "here" : "", click ? "click" : ""].filter(Boolean).join(" ");
  const attrs = click ? ` data-bk-game="${esc(shown.id)}" title="${live ? "Watch this game" : "Show this game"}"` : "";
  return `<div class="${cls}"${attrs}><div class="mb-h"><span class="t">${esc(o.label)}</span>${state}</div>`
    + row(o.a, o.aSeed, o.aTbd) + row(o.b, o.bSeed, o.bTbd) + `</div>`;
}
function miniBracketHtml(curId) {
  const sl = bracketSlots();
  if (!sl) return "";
  const champ = sl.champion ? `<div class="mb-m mb-champ won">${crownSvg("", "mb")}<span class="mb-h"><span class="t">Champion</span></span><b>${esc(sl.champion)}</b></div>`
    : `<div class="mb-m mb-champ">${crownSvg("", "mb", true)}<span class="mb-h"><span class="t">Champion</span></span><span class="tbd">To be crowned</span></div>`;
  return `<div class="mb-title"><span>${sl.ko ? "Knockout bracket" : "Road to the final (projected)"}</span>${sl.short ? `<span class="mb-note">${esc(sl.short)}</span>` : ""}</div><div class="mb-grid">`
    + miniMatch(sl.sf1.m, sl.sf1.o, curId) + miniMatch(sl.sf2.m, sl.sf2.o, curId) + miniMatch(sl.fin.m, sl.fin.o, curId)
    + (sl.third ? miniMatch(sl.third.m, sl.third.o, curId) : "") + champ + `</div>`;
}

// Standings strip in the sticky header, for layouts where the full table is not on screen.
function renderTicker() {
  const rows = (data.standings || []).slice().sort((a, b) => (a.rank || 99) - (b.rank || 99));
  const f = fmtInfo(), size = f ? (f.ko_size || (data.config || {}).knockoutSize || 4) : 0;
  const fg = focusId && data.games ? data.games[focusId] : null;
  const html = rows.length ? `<span class="tk-h">Standings</span>` + rows.map((r, i) =>
    `<span class="tk${size && i < size ? " zone" : ""}${fg && (r.name === fg.white || r.name === fg.black) ? " me" : ""}"><i>${r.rank || i + 1}</i>${esc(r.name)}<b>${esc(r.points ?? 0)}</b></span>`).join("") : "";
  setHTML(document.getElementById("ticker"), html);
}

// Compact leaderboard for focus mode: the round-robin table (final once the knockouts start).
function leaderboardHtml(game) {
  const rows = (data.standings || []).slice().sort((a, b) => (a.rank || 99) - (b.rank || 99));
  if (!rows.length) return "";
  const f = fmtInfo(), k = ko();
  const size = f ? (f.ko_size || (data.config || {}).knockoutSize || 4) : 0;
  const seeds = {};
  if (k) for (const sd of k.seeds || []) seeds[sd.name] = sd.seed;
  const title = k ? "Round robin table (final)" : (f ? "Leaderboard" : "Standings");
  const note = k ? "Seeds S1 to S" + size : (f ? `Top ${size} go to the knockouts` : "");
  let html = `<div class="lb-title"><span>${esc(title)}</span>${note ? `<span class="n">${esc(note)}</span>` : ""}</div>`
    + `<div class="lb-list"><div class="lb-r lb-head"><span class="lb-k">#</span><span>Player</span><span class="lb-p">Pts</span><span class="lb-e">Elo</span></div>`;
  rows.forEach((r, i) => {
    const side = r.name === game.white ? "w" : r.name === game.black ? "b" : "";
    const d = Math.round(r.elo_delta || 0);
    const seed = seeds[r.name] ? `<span class="sd" title="Knockout seed ${seeds[r.name]}">S${seeds[r.name]}</span>` : "";
    const crown = k && k.champion === r.name ? crownSvg("cr", "lb" + i) : "";
    html += `<div class="lb-r ${side ? "me" : ""} ${size && i < size ? "zone" : ""}"${side ? ` title="Playing ${side === "w" ? "White" : "Black"} in this game"` : ""}>`
      + `<span class="lb-k">${r.rank || i + 1}</span><span class="lb-n">${side ? `<span class="dot ${side}"></span>` : ""}${esc(r.name)}${ladderTag(r.name)}${seed}${crown}</span>`
      + `<span class="lb-p">${esc(r.points ?? 0)}</span><span class="lb-e">${r.elo ? Math.round(r.elo) : ""}<i class="${d > 0 ? "up" : d < 0 ? "down" : ""}">${d ? (d > 0 ? "+" : "") + d : ""}</i></span></div>`;
    if (size && i === size - 1 && rows.length > size) html += `<div class="lb-cut">&uarr; knockout zone</div>`;
  });
  return html + `</div>`;
}

// ---- auto-focus without commentary: the most interesting live board -----------------------------
function liveClockMs(game, side, now) {
  let clk = game.clocks ? game.clocks[side] : ((data.config || {}).timeControlMs || 0);
  if (game.thinking && game.thinking.side === side && game.thinking.since_epoch_ms) clk -= Math.max(0, now - game.thinking.since_epoch_ms);
  return clk;
}
function boardInterest(game, now) {
  const hit = pairingOf(game.id), pr = hit ? hit.pairing : null;
  const moves = game.moves || [], total = moves.length;
  const ann = (data.annotations || {})[game.id] || {};
  const rank = Math.min(standingRow(game.white).rank || 99, standingRow(game.black).rank || 99);
  const san = total ? String(moves[total - 1].san || "") : "";
  const bad = m => m === "??" || m === "?";
  let tier = 5, why = "the leaders";
  if ((pr && pr.match === "final") || game.match === "final") { tier = 0; why = "the Final"; }
  else if (isArmageddon(game, pr)) { tier = 1; why = "Armageddon"; }
  else if (bad(ann[total]) || bad(ann[total - 1])) { tier = 2; why = "a fresh mistake"; }
  else if (/[+#x]/.test(san)) { tier = 3; why = /[+#]/.test(san) ? "a check" : "a capture"; }
  else if (Math.min(liveClockMs(game, "white", now), liveClockMs(game, "black", now)) < 60000) { tier = 4; why = "a clock under 1:00"; }
  return { id: game.id, tier, rank, why };
}
function clipDriven(now) {
  return !!(data.commentary && commentaryOn && !needGesture && lastClipAt && (clipPlaying || now - lastClipAt < CLIP_DRIVE_MS));
}
let focusLiveAt = 0;          // last time the auto-focused game was seen live (a finished game stays up a while)
function autoTick(force) {
  if (!autoFocus || !data || !data.games || tour) return;
  const now = Date.now();
  if (clipDriven(now)) { autoReason = "following the commentary"; return; }   // the commentary picks the board
  const live = Object.values(data.games).filter(g => g.status === "live");
  if (!live.length) return;                                     // nothing live: keep what is shown
  const scored = live.map(g => boardInterest(g, now)).sort((a, b) => a.tier - b.tier || a.rank - b.rank || (a.id < b.id ? -1 : 1));
  const best = scored[0];
  const cur = focusId ? scored.find(x => x.id === focusId) : null;
  if (cur) focusLiveAt = now;
  if (cur && best.tier >= cur.tier) { autoReason = cur.why; return; }   // as interesting as any: stay
  const since = cur ? lastAutoSwitch : Math.max(lastAutoSwitch, focusLiveAt);
  if (!force && focusId && data.games[focusId] && now - since < AUTO_GAP_MS) return;
  autoReason = best.why;
  lastAutoSwitch = now;
  setFocus(best.id, { auto: true });
}

// ---- opening hook: a 6 second title card --------------------------------------------------
let polls = 0;
let introEl = null, introDone = false, introTimer = null;
function maybeIntro() {
  if (introDone || !data || !data.id) return;
  const games = Object.values(data.games || {});
  const anyFinished = games.some(g => g.status === "finished");
  const started = games.some(g => g.status === "live" && (g.moves || []).length > 0);
  if (introForced || (!anyFinished && started)) showIntro();
  else if (anyFinished) introDone = true;
}
function showIntro() {
  introDone = true;
  const cfg = data.config || {}, f = fmtInfo();
  const names = (data.players || []).map(p => p.name).filter(Boolean);
  const steps = f ? [["", `Round robin: ${f.rr_rounds || cfg.rounds} rounds`], ["ko", `Top ${f.ko_size || 4}: semifinals`], ["ko", "Final"], ["arma", "Draw? Armageddon decides"]]
    : [["", `Swiss: ${cfg.rounds || ""} rounds`], ["ko", "Most points takes the crown"]];
  const fmtHtml = steps.map(([cls, t], i) => (i ? `<span class="arr" style="--i:${i * 2 - 1}">&rarr;</span>` : "") + `<span class="${cls}" style="--i:${i * 2}">${esc(t)}</span>`).join("");
  const grid = names.map((nm, i) => {
    const dx = (i % 2 ? 1 : -1) * (30 + (i * 17) % 40), dy = ((i * 37) % 70) - 35, r = ((i * 53) % 50) - 25;
    return `<div class="intro-p" style="--i:${i};--dx:${dx}vw;--dy:${dy}vh;--r:${r}deg">${esc(nm)}</div>`;
  }).join("");
  const el = document.createElement("div");
  el.className = "intro";
  el.dataset.intro = "";
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-label", "Tournament intro, click or press Escape to skip");
  el.innerHTML = `<div class="intro-kicker">${esc(data.title || "AI Chess")}</div>`
    + `<h2 class="intro-head"><span class="a">${names.length ? names.length + " AIs." : "The AIs."}</span> <span class="b">1 crown.</span></h2>`
    + `<div class="intro-grid">${grid}</div><div class="intro-fmt">${fmtHtml}</div><div class="intro-skip">Click or press Esc to skip</div><div class="intro-bar"></div>`;
  document.body.appendChild(el);
  introEl = el;
  introTimer = setTimeout(hideIntro, 6000);
}
function hideIntro() {
  if (!introEl) return;
  clearTimeout(introTimer);
  const el = introEl;
  introEl = null;
  el.classList.add("leaving");
  setTimeout(() => el.remove(), 380);
}

// ---- director: time-lapse tours of every board ---------------------------------------------------
// The host only speaks when something matters. After TOUR_QUIET_S seconds without a line, or every
// TOUR_EVERY_MS, the page visits every live board for TOUR_DWELL_MS each under a "time lapse" ribbon
// (the recorder plays these spans TOUR_SPEED times faster), the host is held, and afterwards it sums up
// what changed. Events for the recorder go out as "acl-director" window events.
// ?tourDwell=<ms>&tourEvery=<ms> shorten the tour for testing.
const TOUR_SPEED = 10, TOUR_DWELL_MS = Number(params.get("tourDwell")) || 30000, TOUR_EVERY_MS = Number(params.get("tourEvery")) || 360000;
const TOUR_QUIET_S = 18, TOUR_MIN_BOARDS = 3;
const TOUR_PENDING_MAX_MS = 45000;
let tour = null;               // { ids, i, dwellAt, why }
let tourPending = false;       // a tour is due: the line playing now finishes, then the tour starts
let tourPendingAt = 0;
let lastTourEnd = 0;
let roundSeen = null, roundSeenAt = Date.now();
let quietS = 0;                // seconds the host reports it has had nothing to say
function director(detail) {
  try { window.dispatchEvent(new CustomEvent("acl-director", { detail })); } catch (e) { /* ignore */ }
}
function tourBoards() {
  return Object.values(data.games || {}).filter(g => g.status === "live" && (g.moves || []).length > 0)
    .sort((a, b) => (a.board || 0) - (b.board || 0)).map(g => g.id);
}
function tourReady() {
  return autoFocus && !(focusId && focusPinned) && !introEl && !rcardEl && !needGesture && document.getElementById("champOverlay").hidden;
}
function directorTick() {
  if (!data || !data.id) return;
  if (data.current_round !== roundSeen) { roundSeen = data.current_round; roundSeenAt = Date.now(); }
  if (HOSTED) { tourPending = false; return; }       // the public page never tours (the tour is a recorder feature)
  if (tour) { runTour(); return; }
  const ids = tourBoards();
  if (!tourReady() || ids.length < TOUR_MIN_BOARDS || !stateMoving()) { tourPending = false; return; }
  const now = Date.now();
  const due = now - Math.max(lastTourEnd, roundSeenAt) >= TOUR_EVERY_MS;
  const quiet = !!(data.commentary && commentaryOn) && quietS >= TOUR_QUIET_S && !clipPlaying && !commentaryQueue.length;
  if (!due && !quiet) { tourPending = false; return; }
  if (clipPlaying) {                                       // let the line finish; no new line starts meanwhile
    if (!tourPending) { tourPending = true; tourPendingAt = now; }
    if (now - tourPendingAt < TOUR_PENDING_MAX_MS) return;
  }
  startTour(ids, quiet ? "quiet" : "due");
}
function startTour(ids, why) {
  tour = { ids, i: 0, dwellAt: Date.now(), why };
  tourPending = false;
  if (!HOSTED) fetch(`${API_BASE}/api/commentary/tour?on=1`, { method: "POST", cache: "no-store" }).catch(() => {});
  director({ k: "tour", a: "start", speed: TOUR_SPEED, boards: ids, why });
  tourShow(0);
}
function tourShow(i) {
  tour.i = i;
  tour.dwellAt = Date.now();
  const id = tour.ids[i];
  autoReason = "time lapse tour";
  lastAutoSwitch = Date.now();
  setFocus(id, { auto: true });
  director({ k: "tour", a: "board", game: id, n: i + 1, of: tour.ids.length });
  renderRibbon();
}
function runTour() {
  if (!tourReady()) { endTour("interrupted"); return; }
  if (Date.now() - tour.dwellAt < TOUR_DWELL_MS) { renderRibbon(); return; }
  let i = tour.i + 1;
  while (i < tour.ids.length && (data.games[tour.ids[i]] || {}).status !== "live") i++;
  if (i >= tour.ids.length) { endTour("done"); return; }
  tourShow(i);
}
function endTour(why) {
  if (!tour) return;
  tour = null;
  lastTourEnd = Date.now();
  lastAutoSwitch = 0;
  if (!HOSTED) fetch(`${API_BASE}/api/commentary/tour?on=0`, { method: "POST", cache: "no-store" }).catch(() => {});
  director({ k: "tour", a: "end", why });
  renderRibbon();
}
function renderRibbon() {
  const el = document.getElementById("lapse");
  if (!tour) { el.hidden = true; return; }
  const g = (data.games || {})[tour.ids[tour.i]] || {};
  const pct = Math.min(100, (Date.now() - tour.dwellAt) / TOUR_DWELL_MS * 100);
  el.hidden = false;
  setHTML(el, `<span class="ff">&raquo;&raquo;</span><span>TIME LAPSE x${TOUR_SPEED}</span><span class="of">Board ${tour.i + 1} of ${tour.ids.length}</span>`
    + `<span class="names">${esc(g.white || "")} vs ${esc(g.black || "")}</span><span class="bar"><i style="width:${pct.toFixed(0)}%"></i></span>`);
}

// ---- round preview and round results ---------------------------------------------------------
// Every round robin round opens with a preview card (pairings, the match of the round, the table)
// and closes with a results card (results, the new table, the next round's headline match). The
// card waits up to 25 s for the host's matching line (round-N / recap-N) and stays while it plays.
const roundCardForced = (location.hash.match(/(?:^#|[#&])round-(intro|results)\b/) || [])[1] || "";
const roundCardsShown = new Set();
let rcardEl = null, rcardKey = "", rcardTimer = null, rcardShownAt = 0, rcardMode = "", rcardRound = 0;
const RCARD_MIN_MS = 11000, RCARD_WAIT_MS = 25000, RCARD_MAX_MS = 55000;
const heardEvents = new Set();   // host lines (event keys) that started playing on this page
let serverSkewMs = 0;         // server clock minus this page's clock
function rrRounds() { return (data.rounds || []).filter(r => !r.stage); }
function stateMoving() { const age = stateAgeS(); return typeof age === "number" ? age < 240 : true; }

// ---- state freshness, hosted banner and publish chip ---------------------------------------------
// The hosted page can show a state that is hours old (the laptop that runs the tournament is off), so
// the age keeps growing between answers: state_age_s from the last answer + the time since it arrived.
let stateAt = 0;              // when the last /api/tournament answer arrived (this page's clock)
let feedFailed = false;       // hosted: the last poll got no answer (relay or function unreachable)
function stateAgeS() {
  if (!data || typeof data.state_age_s !== "number") return null;
  return HOSTED && stateAt ? data.state_age_s + (Date.now() - stateAt) / 1000 : data.state_age_s;
}
function lastUpdateText() {
  let ms = Number(data && data.updated_epoch_ms) || 0;
  if (!ms && data && data.updated_at) ms = Date.parse(data.updated_at) || 0;
  if (!ms) return "an unknown time";
  const d = new Date(ms);
  try { return d.toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); }
  catch (e) { return d.toLocaleString(); }
}
function renderBanner() {
  const el = HOSTED ? document.getElementById("hostedBanner") : null;
  if (!el) return;
  let cls, msg;
  const k = data && data.id ? ko() : null;
  const champ = (k && k.champion) || (data && data.winner) || "";
  if (data && data.id && data.finished && champ) {
    cls = "final"; msg = `<b>Final result</b> - champion ${esc(champ)}`;
  } else if (!data || !data.id) {
    cls = feedFailed ? "off" : "wait";
    msg = feedFailed ? "Offline: the tournament feed cannot be reached right now" : "Connecting to the tournament...";
  } else {
    const age = stateAgeS();
    if (typeof age === "number" && age < 90) { cls = "live"; msg = "Live from the tournament"; }
    else { cls = "off"; msg = `Offline: showing the last update from ${esc(lastUpdateText())}`; }
  }
  const name = `hosted-banner ${cls}`;
  if (el.className !== name) el.className = name;
  const hear = data && data.commentary && commentaryOn && needGesture ? `<span class="hear">Commentary is on - click anywhere to hear it</span>` : "";
  setHTML(el, `<span class="dot" aria-hidden="true"></span><span class="msg">${msg}</span>${hear}`);
}
function publishChip() {
  const p = !HOSTED && data ? data.publish : null;
  if (!p || typeof p !== "object") return "";
  const age = typeof p.push_age_s === "number" ? p.push_age_s : null;
  const live = !!(p.ok && p.relay_up && (age === null || age < 90));
  const bits = [];
  if (age !== null) bits.push(`last state push ${Math.round(age)} s ago`);
  if (typeof p.clips_pushed === "number") bits.push(`${p.clips_pushed} clips pushed`);
  if (p.error) bits.push(`error: ${p.error}`);
  const title = (live ? "The public page is receiving this tournament" : "The public page is not receiving updates") + (bits.length ? ` (${bits.join(", ")})` : "");
  const inner = `Published: <b>${live ? "live" : "offline"}</b>`;
  const cls = `chip pub ${live ? "live" : "off"}`;
  return p.url ? `<a class="${cls}" href="${esc(p.url)}" target="_blank" rel="noopener" title="${esc(title)}">${inner}</a>`
    : `<span class="${cls}" title="${esc(title)}">${inner}</span>`;
}
function maybeRoundCard() {
  if (!data || !data.id || introEl || rcardEl) return;
  const games = data.games || {}, rr = rrRounds();
  const tourOnly = !!tour;           // a tour in progress gives way to a results card, never to a preview
  const cur = rr.find(r => r.round === data.current_round);
  if (roundCardForced && !roundCardsShown.has("forced")) {
    roundCardsShown.add("forced");
    const done = rr.filter(r => r.status === "finished");
    if (roundCardForced === "results" && done.length) showRoundCard("results", done[done.length - 1]);
    else if (cur) showRoundCard("intro", cur);
    return;
  }
  // Results first: the round that just ended (seen live, not on a page opened long after).
  const fin = rr.filter(r => r.status === "finished");
  const last = fin[fin.length - 1];
  if (last && !roundCardsShown.has("recap-" + last.round)) {
    const ends = (last.pairings || []).map(p => Date.parse((games[p.game_id] || {}).end || "")).filter(x => !isNaN(x));
    const ago = ends.length ? Date.now() + serverSkewMs - Math.max(...ends) : Infinity;
    if (ago < 300000 && polls > 1) { roundCardsShown.add("recap-" + last.round); endTour("round over"); showRoundCard("results", last); return; }
    if (ago >= 300000) roundCardsShown.add("recap-" + last.round);
  }
  if (cur && cur.round > 1 && !roundCardsShown.has("round-" + cur.round)) {
    const rg = (cur.pairings || []).map(p => games[p.game_id] || {});
    const anyDone = rg.some(g => g.status === "finished");
    const maxPly = Math.max(0, ...rg.map(g => (g.moves || []).length));
    if (anyDone || maxPly > 30) { roundCardsShown.add("round-" + cur.round); return; }   // joined mid round
    if (tourOnly) return;
    // The first move of the round, not its pairing: a round paired and then paused is not previewed.
    if (stateMoving() && maxPly >= 1 && rg.some(g => g.status === "live")) { roundCardsShown.add("round-" + cur.round); showRoundCard("intro", cur); return; }
  }
  maybeKoCard();
}
// Knockouts: one card when the semifinals start and one for the final (and third place game).
function maybeKoCard() {
  const k = ko(), stage = data.stage;
  if (!k || (stage !== "semifinals" && stage !== "final") || roundCardsShown.has("ko-" + stage) || tour) return;
  const games = data.games || {};
  const ms = (k.matches || []).filter(m => m.stage === stage);
  const gs = ms.flatMap(m => m.games || []).map(id => games[id] || {});
  if (!gs.length) return;
  const maxPly = Math.max(0, ...gs.map(g => (g.moves || []).length));
  if (maxPly < 1 && gs.every(g => g.status !== "finished")) return;       // wait for the first move
  roundCardsShown.add("ko-" + stage);
  if (maxPly <= 30 && stateMoving() && gs.some(g => g.status === "live") && !gs.some(g => g.status === "finished")) showKoCard(stage, ms);
}
function fmtClock(ms) { const s = Math.round((ms || 0) / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }
function showKoCard(stage, ms) {
  const k = ko(), cfg = data.config || {};
  const seed = n => (k.seeds || []).find(x => x.name === n) || {};
  const pl = n => `${esc(n)}<small>${seed(n).seed ? `Seed ${esc(seed(n).seed)} - ${esc(seed(n).points)} pts` : ""}</small>`;
  const rows = ms.map((m, i) => `<div class="rc-m${m.id === "final" ? " star" : ""}" style="--i:${i}">`
    + (m.id === "final" ? `<span class="tag">For the crown</span>` : "")
    + `<span class="bd">${esc(m.label || m.id)}</span><span class="pl">${pl(m.a)}</span><span class="vs">vs</span><span class="pl b">${pl(m.b)}</span></div>`).join("");
  const arma = `Lose and you are out. A drawn game goes to an Armageddon decider: White gets ${fmtClock(cfg.armageddonWhiteMs || 600000)}, `
    + `Black gets ${fmtClock(cfg.armageddonBlackMs || 450000)}, and a draw sends Black through.`;
  const head = stage === "semifinals" ? `<span>Semifinals</span>` : `<span>The</span> <span class="gold">final</span>`;
  const el = document.createElement("div");
  el.className = "rcard";
  el.dataset.rcard = "ko";
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-label", "Knockout stage, click or press Escape to skip");
  el.style.setProperty("--dur", RCARD_MIN_MS / 1000 + "s");
  el.innerHTML = `<div class="rc-kicker">${esc(stage === "semifinals" ? "The round robin is over. The knockouts begin" : "One game for the crown")}</div><h2 class="rc-head">${head}</h2>`
    + `<div class="rc-sub">${esc(arma)}</div>`
    + `<div class="rc-body"><div class="rc-col"><h3>${stage === "semifinals" ? "The matches" : "The games"}</h3>${rows}</div>`
    + `<div class="rc-col"><h3>Round robin table (final)</h3>${rcTable(null)}</div></div>`
    + `<div class="rc-skip">Click or press Esc to skip</div><div class="rc-bar"></div>`;
  document.body.appendChild(el);
  rcardEl = el;
  rcardKey = stage === "semifinals" ? "knockouts" : "final";
  rcardMode = "ko";
  rcardRound = data.current_round;
  rcardShownAt = Date.now();
  director({ k: "rcard", a: "show", mode: "ko", round: rcardRound });
  clearTimeout(rcardTimer);
  rcardTimer = setTimeout(tickRoundCard, RCARD_MIN_MS);
}
function rcPlayer(name, extra = "") {
  const r = standingRow(name);
  const meta = r.rank ? `#${r.rank} - ${r.points ?? 0} pt${r.points === 1 ? "" : "s"}` : "";
  return `${esc(name)}${meta || extra ? `<small>${esc(meta)}${extra}</small>` : ""}`;
}
function rcTable(changed) {
  const rows = (data.standings || []).slice().sort((a, b) => (a.rank || 99) - (b.rank || 99));
  const f = fmtInfo(), size = f ? (f.ko_size || (data.config || {}).knockoutSize || 4) : 0;
  let html = `<div class="rc-r head"><span class="k">#</span><span>Player</span><span class="p">Pts</span><span class="wdl">W-D-L</span></div>`;
  rows.forEach((r, i) => {
    const gain = changed ? changed.get(r.name) : 0;
    html += `<div class="rc-r${size && i < size ? " zone" : ""}"><span class="k">${r.rank || i + 1}</span><span class="n">${esc(r.name)}</span>`
      + `<span class="p">${esc(r.points ?? 0)}${gain ? `<span class="gain" title="This round">+${gain === 0.5 ? "&frac12;" : esc(gain)}</span>` : ""}</span><span class="wdl">${r.wins || 0}-${r.draws || 0}-${r.losses || 0}</span></div>`;
    if (size && i === size - 1 && rows.length > size) html += `<div class="rc-cut">top ${size} reach the knockouts</div>`;
  });
  return `<div class="rc-t">${html}</div>`;
}
function showRoundCard(mode, rnd) {
  const games = data.games || {}, f = fmtInfo(), cfg = data.config || {};
  const total = f ? (f.rr_rounds || cfg.rounds) : cfg.rounds;
  const pairs = rnd.pairings || [];
  const rankOf = n => standingRow(n).rank || 99;
  // Match of the round: the two best-placed players meeting (lowest rank sum).
  const star = pairs.slice().sort((a, b) => (rankOf(a.white) + rankOf(a.black)) - (rankOf(b.white) + rankOf(b.black)))[0];
  let left = "", sub = "", next = "", changed = null;
  if (mode === "intro") {
    left = `<h3>The matches</h3>` + pairs.map((p, i) => `<div class="rc-m${p === star ? " star" : ""}" style="--i:${i}">`
      + (p === star ? `<span class="tag">Match of the round</span>` : "")
      + `<span class="bd">Board ${esc(p.board)}</span><span class="pl">${rcPlayer(p.white)}</span><span class="vs">vs</span><span class="pl b">${rcPlayer(p.black)}</span></div>`).join("");
    const leader = (data.standings || []).find(r => r.rank === 1);
    sub = (leader ? `Leader: <b>${esc(leader.name)}</b> on ${esc(leader.points)} - ` : "") + (f ? `top ${f.ko_size || 4} after round ${total} reach the knockouts` : "");
  } else {
    changed = new Map();
    left = `<h3>Results</h3>` + pairs.map((p, i) => {
      const g = games[p.game_id] || {}, res = g.result || "*";
      const ww = res === "1-0", bw = res === "0-1";
      if (ww) changed.set(p.white, 1); if (bw) changed.set(p.black, 1);
      if (res === "1/2-1/2") { changed.set(p.white, 0.5); changed.set(p.black, 0.5); }
      const score = res === "1/2-1/2" ? "&frac12;-&frac12;" : esc(res);
      const how = String(g.termination || "").replace(/^.*?\b(lost on time|checkmate|stalemate|forfeit\w*|resign\w*|draw by [a-z ]+|threefold repetition|insufficient material|fifty-move rule|invalid replies)\b.*$/i, "$1");
      return `<div class="rc-m" style="--i:${i}"><span class="bd">Board ${esc(p.board)}</span>`
        + `<span class="pl ${ww ? "won" : bw ? "lost" : ""}">${esc(p.white)}</span><span class="vs res">${score}</span><span class="pl b ${bw ? "won" : ww ? "lost" : ""}">${esc(p.black)}</span>`
        + (how ? `<span class="how">${esc(how)}</span>` : "") + `</div>`;
    }).join("");
    const nx = rrRounds().find(r => r.round === rnd.round + 1);
    const top = nx && (nx.pairings || []).slice().sort((a, b) => (rankOf(a.white) + rankOf(a.black)) - (rankOf(b.white) + rankOf(b.black)))[0];
    if (top) next = `<div class="rc-next">Next: round ${nx.round} - <b>${esc(top.white)}</b> vs <b>${esc(top.black)}</b></div>`;
    else if (f && rnd.round >= total) next = `<div class="rc-next">Next: <b>the knockouts</b></div>`;
    const leader = (data.standings || []).find(r => r.rank === 1);
    sub = leader ? `<b>${esc(leader.name)}</b> leads on ${esc(leader.points)}` + (total && total > rnd.round ? ` - ${total - rnd.round} round${total - rnd.round === 1 ? "" : "s"} to go` : "") : "";
  }
  const head = mode === "intro"
    ? `<span>Round ${esc(rnd.round)}</span>${total ? `<span class="of">of ${esc(total)}</span>` : ""}`
    : `<span>Round ${esc(rnd.round)}</span> <span class="gold">results</span>`;
  const el = document.createElement("div");
  el.className = "rcard";
  el.dataset.rcard = mode;
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-label", `Round ${rnd.round} ${mode === "intro" ? "preview" : "results"}, click or press Escape to skip`);
  el.style.setProperty("--dur", RCARD_MIN_MS / 1000 + "s");
  el.innerHTML = `<div class="rc-kicker">${esc(data.title || "AI Chess")}</div><h2 class="rc-head">${head}</h2>`
    + (sub ? `<div class="rc-sub">${sub}</div>` : "")
    + `<div class="rc-body"><div class="rc-col">${left}</div><div class="rc-col"><h3>${mode === "intro" ? "The table" : "The table now"}</h3>${rcTable(changed)}</div></div>`
    + next + `<div class="rc-skip">Click or press Esc to skip</div><div class="rc-bar"></div>`;
  document.body.appendChild(el);
  rcardEl = el;
  rcardKey = (mode === "intro" ? "round-" : "recap-") + rnd.round;
  rcardMode = mode;
  rcardRound = rnd.round;
  rcardShownAt = Date.now();
  director({ k: "rcard", a: "show", mode, round: rnd.round });
  clearTimeout(rcardTimer);
  rcardTimer = setTimeout(tickRoundCard, RCARD_MIN_MS);
}
function tickRoundCard() {
  if (!rcardEl) return;
  const age = Date.now() - rcardShownAt;
  // Stay while the host's line for this round is still queued or playing.
  const talking = (clipPlaying && caption && caption.event === rcardKey) || commentaryQueue.some(c => c.event === rcardKey);
  const coming = data.commentary && commentaryOn && !heardEvents.has(rcardKey) && age < RCARD_WAIT_MS;   // still being voiced
  if (age >= RCARD_MAX_MS || (age >= RCARD_MIN_MS && !talking && !coming)) { hideRoundCard(); return; }
  rcardTimer = setTimeout(tickRoundCard, 500);
}
function hideRoundCard() {
  if (!rcardEl) return;
  clearTimeout(rcardTimer);
  const el = rcardEl;
  rcardEl = null;
  director({ k: "rcard", a: "hide", mode: rcardMode, round: rcardRound });
  el.classList.add("leaving");
  setTimeout(() => el.remove(), 380);
}

// ---- champion moment ------------------------------------------------------------------------
let champSeen = null;
let champTimer = 0;
const CHAMP_AUTOCLOSE_MS = 20000;
function checkChampion() {
  const k = ko();
  const name = k && k.champion;
  if (!name || champSeen === name) return;
  champSeen = name;
  // Crowned while watching (or #champion): play it. Page opened later: show it without the show.
  showChampion(polls > 1 || champForced);
}
function champHtml(k) {
  const seed = (k.seeds || []).find(s => s.name === k.champion);
  const fin = (k.matches || []).find(m => m.id === "final");
  let line = "";
  if (fin) {
    const done = matchGameList(fin).filter(g => g.result && g.result !== "*");
    const g = done[done.length - 1];
    if (g) line = `Final: ${g.white} ${g.result === "1/2-1/2" ? "½-½" : g.result} ${g.black}${fin.decided_by === "armageddon" ? ", Armageddon decider" : ""}${g.termination ? ` (${g.termination})` : ""}`;
  }
  return crownSvg("champ-crown", "big")
    + `<div class="champ-kicker">${esc(data.title || "AI Chess")}</div>`
    + `<div class="champ-name" id="champName">${esc(k.champion)}</div><div class="champ-sub">is the champion</div>`
    + (seed ? `<div class="champ-seed">Seed ${seed.seed} after the round robin${seed.points !== undefined && seed.points !== null ? `, ${seed.points} points` : ""}</div>` : "")
    + `<div class="podium">${k.runner_up ? `<div class="silver"><div class="lbl">Runner-up</div><div class="nm">${esc(k.runner_up)}</div></div>` : ""}`
    + `${k.third ? `<div class="bronze"><div class="lbl">Third place</div><div class="nm">${esc(k.third)}</div></div>` : ""}</div>`
    + (line ? `<p class="champ-line">${esc(line)}</p>` : "")
    + `<button type="button" class="champ-btn" data-close-champion>Back to the boards</button>`;
}
function showChampion(animate) {
  const k = ko();
  if (!k || !k.champion) return;
  const ov = document.getElementById("champOverlay");
  document.getElementById("champCard").innerHTML = champHtml(k);
  ov.classList.toggle("play", !!animate);
  ov.hidden = false;
  director({ k: "champion", a: "show" });
  clearTimeout(champTimer);
  // Unattended screens (the stream, a viewer following the forever runner) close it by themselves.
  if (STREAM || (data && data.viewer_follow)) champTimer = setTimeout(hideChampion, CHAMP_AUTOCLOSE_MS);
  stopConfetti();
  let still = false;
  try { still = matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { /* old browser */ }
  if (animate && !still) startConfetti();
}
function hideChampion() {
  clearTimeout(champTimer);
  if (!document.getElementById("champOverlay").hidden) director({ k: "champion", a: "hide" });
  document.getElementById("champOverlay").hidden = true;
  stopConfetti();
}
let confettiRaf = 0;
function stopConfetti() {
  if (confettiRaf) cancelAnimationFrame(confettiRaf);
  confettiRaf = 0;
  const cv = document.getElementById("confetti");
  const ctx = cv.getContext && cv.getContext("2d");
  if (ctx) ctx.clearRect(0, 0, cv.width, cv.height);
}
function startConfetti() {
  const cv = document.getElementById("confetti");
  const ctx = cv.getContext && cv.getContext("2d");
  if (!ctx) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  cv.width = innerWidth * dpr; cv.height = innerHeight * dpr;
  cv.style.opacity = "1";
  const colors = ["#f5b942", "#ffd479", "#5b9dff", "#34c77b", "#ff5d5d", "#eef1f5", "#c58bff"];
  const parts = [];
  const spawn = (n, burst) => {
    for (let i = 0; i < n; i++) {
      const left = Math.random() < 0.5;
      const p = burst
        ? { x: left ? -10 : innerWidth + 10, y: innerHeight * (0.55 + Math.random() * 0.3), vx: (left ? 1 : -1) * (5 + Math.random() * 10), vy: -(9 + Math.random() * 11) }
        : { x: Math.random() * innerWidth, y: -20 - Math.random() * 60, vx: (Math.random() - 0.5) * 2, vy: 1.5 + Math.random() * 2.5 };
      Object.assign(p, { w: 6 + Math.random() * 6, h: 9 + Math.random() * 8, rot: Math.random() * 6.3, vr: (Math.random() - 0.5) * 0.3,
        phase: Math.random() * 6.3, c: colors[(Math.random() * colors.length) | 0], round: Math.random() < 0.2 });
      parts.push(p);
    }
  };
  spawn(160, true);
  const t0 = performance.now();
  let last = t0;
  const frame = t => {
    const age = (t - t0) / 1000, dt = Math.min(2.5, (t - last) / 16.67);
    last = t;
    if (age < 6) spawn(Math.random() < 0.6 ? 2 : 1, false);
    if (age > 8.2 && cv.style.opacity !== "0") cv.style.opacity = "0";     // settles: fades out over ~1.2 s
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    for (let i = parts.length - 1; i >= 0; i--) {
      const p = parts[i];
      p.vx *= Math.pow(0.985, dt);
      p.vy = Math.min(p.vy + 0.2 * dt, 4.5 + p.h * 0.1);
      p.x += (p.vx + Math.sin(p.phase + age * 3) * 0.7) * dt;
      p.y += p.vy * dt;
      p.rot += p.vr * dt;
      if (p.y > innerHeight + 30) { parts.splice(i, 1); continue; }
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rot);
      ctx.scale(1, Math.cos(p.rot * 1.7));
      ctx.fillStyle = p.c;
      if (p.round) { ctx.beginPath(); ctx.arc(0, 0, p.w / 2, 0, 6.283); ctx.fill(); } else ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
      ctx.restore();
    }
    if (age < 9.6) confettiRaf = requestAnimationFrame(frame);
    else { ctx.clearRect(0, 0, innerWidth, innerHeight); confettiRaf = 0; }
  };
  confettiRaf = requestAnimationFrame(frame);
}

// ---- move sound: a short wooden click, synthesized (no audio files) -------------------------
let audioCtx = null;
function unlockAudio() {
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
  } catch (e) { audioCtx = null; }
}
function playClick(delay) {
  if (!soundOn || !audioCtx || audioCtx.state !== "running") return;
  try {
    const t = audioCtx.currentTime + (delay || 0);
    const len = Math.floor(audioCtx.sampleRate * 0.045);
    const buf = audioCtx.createBuffer(1, len, audioCtx.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 5);
    const src = audioCtx.createBufferSource(); src.buffer = buf;
    const bp = audioCtx.createBiquadFilter(); bp.type = "bandpass"; bp.frequency.value = 1800; bp.Q.value = 2.5;
    const g = audioCtx.createGain(); g.gain.setValueAtTime(0.7, t); g.gain.exponentialRampToValueAtTime(0.001, t + 0.05);
    src.connect(bp); bp.connect(g); g.connect(audioCtx.destination); src.start(t);
    const o = audioCtx.createOscillator(); o.type = "sine";
    o.frequency.setValueAtTime(260, t); o.frequency.exponentialRampToValueAtTime(120, t + 0.07);
    const g2 = audioCtx.createGain(); g2.gain.setValueAtTime(0.35, t); g2.gain.exponentialRampToValueAtTime(0.001, t + 0.08);
    o.connect(g2); g2.connect(audioCtx.destination); o.start(t); o.stop(t + 0.1);
  } catch (e) { /* audio unavailable */ }
}
const moveCounts = {};        // game id -> moves seen at the last poll
function soundForNewMoves() {
  const visible = new Set(shownGames().map(g => g.id));
  let clicks = 0;
  for (const [id, g] of Object.entries(data.games || {})) {
    const n = (g.moves || []).length;
    const prev = moveCounts[id];
    if (prev !== undefined && n > prev && visible.has(id)) clicks++;
    moveCounts[id] = n;
  }
  for (let i = 0; i < Math.min(clicks, 3); i++) playClick(i * 0.09);
}

// ---- commentary player ------------------------------------------------------------------
// The commentator picks the board itself (leaders, interesting games): clips arrive for every
// game in one sequence. Focus mode pins it to the focused board and plays only that board.
let caption = null;           // { game, text } shown under that board
let needGesture = HOSTED && !(navigator.userActivation && navigator.userActivation.hasBeenActive);   // the browser blocks sound until a click
const commentaryQueue = [];
let lastSeq = 0;              // highest clip seq received (all games)
let seqPrimed = false;        // first poll done (older clips of finished games are skipped)
let clipPlaying = false;
let clipTimer = null;
let clipToken = 0;            // bumped by stopClip so a stopped clip's late callbacks are ignored
let focusSent;                // undefined = nothing sent yet; null = auto mode sent; id = pinned
let lastUserScroll = 0;       // last time the viewer scrolled the page themselves
const clipAudio = new Audio();
clipAudio.preload = "auto";
// Count only the viewer's own scrolling (wheel, touch, keys, scrollbar), never our scrollIntoView.
function userScrolled() { lastUserScroll = Date.now(); }
for (const type of ["wheel", "touchmove"]) window.addEventListener(type, userScrolled, { passive: true });
window.addEventListener("keydown", ev => { if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(ev.key)) userScrolled(); });
window.addEventListener("pointerdown", ev => { if (ev.target === document.documentElement) userScrolled(); });   // page scrollbar
function stopClip() {
  clipToken++;
  clearTimeout(clipTimer);
  try { clipAudio.pause(); } catch (e) { /* ignore */ }
  clipPlaying = false;
  caption = null;
}
function clipWanted(clip) {
  const game = (data.games || {})[clip.game];
  if (!game) return false;
  if (focusId && focusPinned && clip.game !== focusId) return false;   // viewer's own focus: only that board
  // Speech must stay on the move it describes: drop a line once its board is 2 moves on, or when
  // it waited too long (a round or champion line may wait for the line before it).
  const age = Date.now() - (clip.got || Date.now());
  if (clip.event) return age < 60000;
  return (game.moves || []).length - (clip.ply || 0) <= 1 && age < 20000;
}
function clipTag(clip) {
  // The move the line is about, so a viewer can tie the words to the board.
  if (clip.thinking) return "thinking now";
  if (clip.event) return "";
  const game = (data.games || {})[clip.game] || {};
  const mv = (game.moves || [])[(clip.ply || 0) - 1];
  return mv ? `${Math.ceil(mv.ply / 2)}${mv.side === "white" ? "." : "..."} ${mv.san}` : "";
}
function syncCommentaryTarget() {
  if (!data || !data.commentary) return;
  if (focusId && focusPinned && caption && caption.game !== focusId) stopClip();   // the viewer focused another board
  // A board the viewer picks pins the commentator; auto-focus and leaving focus hand the choice back.
  const want = focusId && focusPinned && data.games && data.games[focusId] ? focusId : null;
  if (want !== focusSent && !(focusSent === undefined && want === null)) {
    focusSent = want;
    const q = want ? `?game=${encodeURIComponent(want)}` : "";
    // The public page only reads: one viewer must never steer the shared commentator.
    if (!HOSTED) fetch(`${API_BASE}/api/commentary/focus${q}`, { method: "POST", cache: "no-store" }).catch(() => {});
  }
}
function showOnAir(gameId) {
  // Grid only: bring the commented card into view if it is fully off-screen and the
  // viewer has not scrolled in the last 10 s.
  if (focusId || Date.now() - lastUserScroll < 10000) return;
  const c = cards.get(gameId);
  if (!c || !c.el.isConnected) return;
  const r = c.el.getBoundingClientRect();
  if (r.bottom > 0 && r.top < innerHeight) return;
  c.el.scrollIntoView({ block: "nearest", behavior: "smooth" });
}
function clipDone() {
  clearTimeout(clipTimer);
  lastClipAt = Date.now();
  clipPlaying = false;
  caption = null;
  render();
  playNextClip();
}
function playNextClip() {
  if (clipPlaying || needGesture || !commentaryOn) return;
  if (tour || tourPending) return;                      // the host waits for the time-lapse tour
  while (commentaryQueue.length) {
    const clip = commentaryQueue.shift();
    if (!clipWanted(clip)) continue;
    clipPlaying = true;
    lastClipAt = Date.now();
    caption = { game: clip.game, text: clip.text || "", event: clip.event || "", tag: clipTag(clip) };
    if (clip.event) heardEvents.add(clip.event);
    // Auto-focus follows the commentary: show the board this clip is about.
    if (autoFocus && clip.game !== focusId) { autoReason = "following the commentary"; lastAutoSwitch = Date.now(); setFocus(clip.game, { auto: true }); }
    else if (autoFocus) autoReason = "following the commentary";
    render();
    showOnAir(clip.game);
    if (clip.audio) {
      const token = clipToken;
      clipAudio.src = `${API_BASE}/api/commentary/audio/${encodeURIComponent(clip.audio)}`;
      clipAudio.play().catch(err => {
        if (token !== clipToken) return;             // stopped on purpose (mute, focus change)
        if (err && err.name === "NotAllowedError") {
          // Autoplay blocked until a user gesture: keep the clip and wait for the next click.
          clearTimeout(clipTimer);
          commentaryQueue.unshift(clip);
          clipPlaying = false; caption = null; needGesture = true; render();
        } else clipDone();                           // broken clip: skip it
      });
      clipTimer = setTimeout(clipDone, 90000);   // safety net if "ended" never fires
    } else {
      clipTimer = setTimeout(clipDone, Math.max(2500, (clip.text || "").length * 60));
    }
    return;
  }
}
clipAudio.addEventListener("ended", clipDone);
clipAudio.addEventListener("error", () => { if (clipPlaying) clipDone(); });
async function pollCommentary() {
  if (!data || !data.commentary || !commentaryOn) return;
  try {
    const res = await fetch(`${API_BASE}/api/commentary?after=${lastSeq}`, { cache: "no-store" });
    if (!res.ok) return;
    const j = await res.json();
    quietS = Number(j.quiet_s) || 0;                    // seconds the host has had nothing worth saying
    const clips = (j.clips || []).filter(c => typeof c.seq === "number" && c.seq > lastSeq && c.game).sort((a, b) => a.seq - b.seq);
    for (const [i, clip] of clips.entries()) {
      lastSeq = clip.seq;
      clip.got = Date.now() - Math.max(0, Number(clip.age_s) || 0) * 1000;   // when the server made it
      const game = (data.games || {})[clip.game];
      // Page just opened: no backlog, only the newest clip and only for a game still playing.
      if (!seqPrimed && (i < clips.length - 1 || !(game && game.status === "live"))) continue;
      commentaryQueue.push(clip);
    }
    seqPrimed = true;
    playNextClip();
  } catch (e) { /* try again next tick */ }
}
setInterval(pollCommentary, HOSTED ? 1500 : 1000);

// ---- input --------------------------------------------------------------------------------
document.addEventListener("click", ev => {
  unlockAudio();
  if (needGesture) { needGesture = false; playNextClip(); }
  if (ev.target.closest("[data-intro]")) { hideIntro(); return; }
  if (ev.target.closest("[data-rcard]")) { hideRoundCard(); return; }
  if (ev.target.closest("[data-close-champion]")) { hideChampion(); return; }
  if (ev.target.closest("[data-show-champion]")) { showChampion(false); return; }
  if (ev.target.closest("[data-toggle-autofocus]")) { setAutoFocus(!autoFocus); return; }
  const bk = ev.target.closest("[data-bk-game]");
  if (bk) { setFocus(bk.dataset.bkGame); return; }
  if (ev.target.closest("[data-toggle-analysis]")) {
    analysisOn = !analysisOn;
    store("swissAnalysis", analysisOn ? "on" : "off");
    render();
    return;
  }
  if (ev.target.closest("[data-toggle-sound]")) {
    soundOn = !soundOn;
    store("swissMoveSound", soundOn ? "on" : "off");
    render();
    if (soundOn) setTimeout(() => playClick(0), 30);
    return;
  }
  if (ev.target.closest("[data-toggle-commentary]")) {
    commentaryOn = !commentaryOn;
    store("swissCommentary", commentaryOn ? "on" : "off");
    if (!commentaryOn) { commentaryQueue.length = 0; stopClip(); }
    render();
    if (commentaryOn) pollCommentary();
    return;
  }
  if (ev.target.closest("[data-think-toggle]")) {
    const mode = thinkMode();
    thinkOpen[mode] = !thinkOpen[mode];
    store(`swissThinking.${mode}`, thinkOpen[mode] ? "open" : "closed");
    render();
    return;
  }
  if (ev.target.closest("[data-unfocus]")) { setFocus(null); return; }
  const focusBtn = ev.target.closest("[data-focus]");
  if (focusBtn) { setFocus(focusBtn.dataset.focus); return; }
  const head = ev.target.closest("[data-focus-head]");
  if (head && !focusId) { setFocus(head.closest("[data-game]").dataset.game); return; }
  const pick = ev.target.closest("[data-pick]");
  if (pick) { selected = pick.dataset.pick; replayPly = null; render(); return; }
  const mv = ev.target.closest("[data-ply]");
  if (mv) {
    const id = mv.closest("[data-game]").dataset.game;
    selected = id; replayPly = +mv.dataset.ply; render(); return;
  }
  const nav = ev.target.closest("[data-nav]");
  if (!nav) return;
  const card = nav.closest("[data-game]");
  const game = data.games[card.dataset.game];
  const total = (game.moves || []).length;
  if (selected !== game.id) { selected = game.id; replayPly = total; }
  const cur = replayPly === null ? total : replayPly;
  const act = nav.dataset.nav;
  if (act === "close") { selected = null; replayPly = null; }
  else replayPly = act === "first" ? 0 : act === "prev" ? Math.max(0, cur - 1) : act === "next" ? Math.min(total, cur + 1) : total;
  render();
});
document.addEventListener("keydown", ev => {
  if (ev.key === "Escape" && introEl) { hideIntro(); return; }
  if (ev.key === "Escape" && rcardEl) { hideRoundCard(); return; }
  if (ev.key === "Escape" && !document.getElementById("champOverlay").hidden) { hideChampion(); return; }
  if (!data || !data.games) return;
  if (ev.key === "Escape") {
    if (focusId) setFocus(null);
    else if (selected) { selected = null; replayPly = null; render(); }
    return;
  }
  if (focusId && data.games[focusId] && selected !== focusId && (ev.key === "ArrowLeft" || ev.key === "ArrowRight")) { selected = focusId; replayPly = null; }
  if (!selected || !data.games[selected]) return;
  const total = (data.games[selected].moves || []).length;
  const cur = replayPly === null ? total : replayPly;
  if (ev.key === "ArrowLeft") { ev.preventDefault(); replayPly = Math.max(0, cur - 1); render(); }
  if (ev.key === "ArrowRight") { ev.preventDefault(); replayPly = Math.min(total, cur + 1); render(); }
});

// The pointer moved to the next tournament (forever runner): close the champion, drop the old boards
// (game ids like r1b1 repeat in every tournament) and start the new one from a clean page.
function newTournament() {
  hideChampion();
  champSeen = null;
  cards.forEach(c => c.el.remove());
  cards.clear();
  selected = null;
  replayPly = null;
  if (focusId) { focusId = null; document.body.classList.remove("focus-mode"); }
}
async function poll() {
  try {
    let q = params.get("id") ? `?id=${encodeURIComponent(params.get("id"))}` : "";
    // Hosted: ask only for a newer state; the answer is a tiny {"unchanged": true} when nothing moved.
    if (HOSTED && data && data.updated_epoch_ms) q += `${q ? "&" : "?"}since=${encodeURIComponent(data.updated_epoch_ms)}`;
    const res = await fetch(`${API_BASE}/api/tournament${q}`, { cache: "no-store" });
    const sentAt = Date.now();
    if (HOSTED && !res.ok) { feedFailed = true; renderBanner(); return; }
    if (res.ok) {
      const got = await res.json();
      if (HOSTED && got && got.unchanged) {
        feedFailed = false;
        if (!data) return;
        // Same state: only its age fields move on.
        if (typeof got.state_age_s === "number") data.state_age_s = got.state_age_s;
        if (got.server_now_ms) data.server_now_ms = got.server_now_ms;
      } else {
        if (data && got && got.id && data.id && got.id !== data.id) newTournament();
        data = got;
      }
      stateAt = Date.now();
      feedFailed = false;
      if (data.server_now_ms) serverSkewMs = data.server_now_ms - Math.round((sentAt + Date.now()) / 2);
      polls++;
      soundForNewMoves();
      render();
      autoTick(false);
      maybeIntro();
      maybeRoundCard();
      directorTick();
      checkChampion();
      renderBanner();
    }
  } catch (e) {
    if (HOSTED) { feedFailed = true; renderBanner(); }
    /* keep the last frame */
  }
}
// A header that wraps onto more rows (narrow screens) takes height from the focused board, so the
// whole card still fits the screen; a one-row header changes nothing.
(function watchHeader() {
  const hdr = document.querySelector("header"), tick = document.getElementById("ticker");
  const ban = document.getElementById("hostedBanner");   // hosted page only
  const set = () => {
    document.documentElement.style.setProperty("--hdr-extra", Math.max(0, hdr.offsetHeight + tick.offsetHeight + (ban ? ban.offsetHeight : 0) - 60) + "px");
    document.documentElement.style.setProperty("--chrome-h", (hdr.offsetHeight + tick.offsetHeight + (ban ? ban.offsetHeight : 0)) + "px");
  };
  try { const ro = new ResizeObserver(set); ro.observe(hdr); ro.observe(tick); if (ban) ro.observe(ban); } catch (e) { window.addEventListener("resize", set); }
  set();
})();
poll();
setInterval(poll, HOSTED ? 2000 : 1000);
setInterval(render, 500);   // clocks tick between polls; unchanged parts are not touched
if (HOSTED) setInterval(renderBanner, 1000);   // the age grows between answers, also with no data at all
</script>
</body>
</html>
"""


DEFAULT_ENGINE = ROOT / "out" / "engines" / "stockfish-19" / "stockfish" / "stockfish-windows-x86-64-universal.exe"


class SharedEngine:
    """One Stockfish process for both the eval bar (Analyzer) and the move marks (Annotator), one search at a
    time (--one-engine, the VPS default: two processes with large hashes cost about 930 MB of RAM)."""

    def __init__(self, path: Path, threads: int = 1, hash_mb: int = 64) -> None:
        import chess.engine

        self.engine = chess.engine.SimpleEngine.popen_uci(str(path))
        self.engine.configure({"Threads": threads, "Hash": hash_mb})
        self.id = self.engine.id
        self.lock = threading.Lock()

    def analyse(self, *args, **kwargs):
        with self.lock:
            return self.engine.analyse(*args, **kwargs)

    def configure(self, options: dict) -> None:
        pass   # set once at start for both users


class Analyzer:
    """Viewer-only Stockfish analysis (never sent to the players).

    Live positions are searched in short slices, round robin, until they reach the target
    depth; Stockfish's hash makes every slice continue deeper. Replay positions asked for by
    the page join the queue behind the live ones.
    """

    def __init__(self, path: Path, slice_s: float = 0.8, target_depth: int = 26, threads: int = 4, hash_mb: int = 512,
                 shared: "SharedEngine | None" = None) -> None:
        import chess.engine

        if shared is not None:
            self.engine = shared
        else:
            self.engine = chess.engine.SimpleEngine.popen_uci(str(path))
            self.engine.configure({"Threads": threads, "Hash": hash_mb})
        self.name = self.engine.id.get("name", "Stockfish")
        self.slice_s = slice_s
        self.target_depth = target_depth
        self.cache: dict[str, dict] = {}
        self.live: list[str] = []
        self.extra: list[str] = []
        self.lock = threading.Lock()
        self.wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def want(self, live: list[str] | None = None, extra: str | None = None) -> None:
        with self.lock:
            if live is not None:
                self.live = list(dict.fromkeys(live))
            if extra and extra not in self.extra:
                self.extra = (self.extra + [extra])[-6:]
        self.wake.set()

    def get(self, fen: str) -> dict | None:
        with self.lock:
            return self.cache.get(fen)

    def _next(self) -> str | None:
        with self.lock:
            queue = self.live + [f for f in self.extra if f not in self.live]
            pending = [f for f in queue if self.cache.get(f, {}).get("depth", 0) < self.target_depth]
            if not pending:
                return None
            # Shallowest first, so every live board gets a quick number before any goes deep.
            return min(pending, key=lambda f: (self.cache.get(f, {}).get("depth", 0), queue.index(f)))

    def _loop(self) -> None:
        import chess
        import chess.engine

        while True:
            fen = self._next()
            if fen is None:
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            board = chess.Board(fen)
            if board.is_game_over():
                with self.lock:
                    self.cache[fen] = {"fen": fen, "depth": 99, "over": True, "engine": self.name}
                continue
            try:
                info = self.engine.analyse(board, chess.engine.Limit(time=self.slice_s))
            except Exception as exc:  # engine crash: report and stop analysing
                with self.lock:
                    self.cache[fen] = {"fen": fen, "depth": 99, "error": str(exc), "engine": self.name}
                continue
            score = info.get("score")
            white = score.white() if score is not None else None
            pv = info.get("pv") or []
            result = {
                "fen": fen,
                "engine": self.name,
                "depth": int(info.get("depth") or 0),
                "cp": white.score() if white is not None and not white.is_mate() else None,
                "mate": white.mate() if white is not None and white.is_mate() else None,
                "best": board.san(pv[0]) if pv else "",
                "pv": board.variation_san(pv[:8]) if pv else "",
            }
            with self.lock:
                old = self.cache.get(fen)
                if old is None or result["depth"] >= old.get("depth", 0):
                    self.cache[fen] = result
                if len(self.cache) > 4000:
                    for key in list(self.cache)[:1000]:
                        self.cache.pop(key, None)


# ---- move annotations (NAG symbols) ---------------------------------------------------------
# Lichess method: a centipawn score becomes a win chance for the side to move, and a move is
# judged by how much win chance the mover gave away. Only ?? ? ?! and ! are produced; this
# viewer never invents !! (brilliant) or !? (interesting): those need a human judgement that a
# win-chance drop cannot measure.
NAG_DEPTH = 16
NAG_MULTIPV = 2
WIN_K = 0.00368208
MATE_CP = 10000
CP_CLAMP = 1000
BLUNDER_DROP = 30.0
MISTAKE_DROP = 20.0
INACCURACY_DROP = 10.0
GOOD_GAP = 10.0
DECIDED_LOW, DECIDED_HIGH = 10.0, 90.0


def win_percent(cp: float) -> float:
    """Win chance (0-100) for the side whose point of view `cp` is in."""
    cp = max(-CP_CLAMP, min(CP_CLAMP, float(cp)))
    return 50 + 50 * (2 / (1 + math.exp(-WIN_K * cp)) - 1)


def score_cp(cp: int | None, mate: int | None) -> int:
    """One number per score: mate in N for the side to move = +10000, being mated = -10000."""
    if mate is not None:
        return MATE_CP if mate > 0 else -MATE_CP
    return int(cp or 0)


def classify_move(best_cp: int, second_cp: int | None, best_uci: str | None, played_uci: str, after_cp: int) -> str:
    """NAG for one played move. Every score is from the mover's point of view.

    best_cp / second_cp: MultiPV 1 and 2 of the position before the move.
    after_cp: the position after the move (negated side-to-move score).
    """
    before = win_percent(best_cp)
    drop = before - win_percent(after_cp)
    if played_uci != best_uci:  # the engine's own first choice is never called a mistake
        if drop >= BLUNDER_DROP:
            return "??"
        if drop >= MISTAKE_DROP:
            return "?"
        if drop >= INACCURACY_DROP:
            return "?!"
        return ""
    if second_cp is None or not (DECIDED_LOW <= before <= DECIDED_HIGH):
        return ""
    if before - win_percent(second_cp) >= GOOD_GAP:
        return "!"
    return ""


def annotate_plies(moves_uci: list[str], records: list[dict | None]) -> dict[str, str]:
    """{ply: mark} for every move whose before and after positions are analysed.

    records[i] is the position after i plies: {"cp", "second", "best", "over"} with scores
    from the side to move's point of view.
    """
    out: dict[str, str] = {}
    for ply in range(1, len(moves_uci) + 1):
        before = records[ply - 1] if ply - 1 < len(records) else None
        after = records[ply] if ply < len(records) else None
        if not before or not after or before.get("over"):
            continue
        mark = classify_move(int(before["cp"]), before.get("second"), before.get("best"), moves_uci[ply - 1], -int(after["cp"]))
        if mark:
            out[str(ply)] = mark
    return out


def state_slug(state_path: Path) -> str:
    name = Path(state_path).name
    return name[: -len("-tournament.json")] if name.endswith("-tournament.json") else Path(state_path).stem


def annotations_path(state_path: Path) -> Path:
    return Path(state_path).parent / f"{state_slug(state_path)}-annotations.json"


class Annotator:
    """Marks every played move of every game in the background (viewer only).

    A separate Stockfish process from Analyzer, fixed depth with MultiPV 2. Position results
    live in memory and in <slug>-annotations.json next to the state file, so a restart only
    analyses positions it has not seen.
    """

    def __init__(self, path: Path, depth: int = NAG_DEPTH, threads: int = 2, hash_mb: int = 128,
                 shared: "SharedEngine | None" = None) -> None:
        self.shared = shared
        self.engine_path = Path(path)
        self.depth = depth
        self.threads = threads
        self.hash_mb = hash_mb
        self.name = "Stockfish"
        self.error = ""
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.positions: dict[Path, dict[str, dict]] = {}      # state path -> fen -> record
        self.games: dict[Path, dict[str, dict]] = {}          # state path -> game id -> {uci, fens, live, order}
        self.dirty: dict[Path, int] = {}
        self.started = False

    def start(self) -> "Annotator":
        if not self.started:
            self.started = True
            threading.Thread(target=self._loop, daemon=True).start()
        return self

    # -- sidecar ----------------------------------------------------------------------------
    def _load(self, state_path: Path) -> None:
        if state_path in self.positions:
            return
        known: dict[str, dict] = {}
        side = annotations_path(state_path)
        try:
            saved = json.loads(side.read_text(encoding="utf-8"))
            if int(saved.get("depth") or 0) >= self.depth:
                known = dict(saved.get("positions") or {})
        except (OSError, ValueError):
            pass
        self.positions[state_path] = known

    def save(self, state_path: Path) -> None:
        with self.lock:
            positions = dict(self.positions.get(state_path) or {})
            self.dirty[state_path] = 0
        side = annotations_path(state_path)
        try:  # merge with whatever another viewer instance wrote meanwhile
            saved = json.loads(side.read_text(encoding="utf-8"))
            if int(saved.get("depth") or 0) == self.depth:
                positions = {**(saved.get("positions") or {}), **positions}
        except (OSError, ValueError):
            pass
        payload = {"engine": self.name, "depth": self.depth, "multipv": NAG_MULTIPV, "updated_epoch_ms": int(time.time() * 1000), "positions": positions}
        tmp = side.with_name(f"{side.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, side)
        except OSError as exc:
            print(f"annotations: could not write {side}: {exc}", flush=True)

    # -- requests ---------------------------------------------------------------------------
    def watch(self, state_path: Path, state: dict) -> None:
        import chess

        state_path = Path(state_path)
        with self.lock:
            self._load(state_path)
            games = self.games.setdefault(state_path, {})
            for order, (gid, game) in enumerate((state.get("games") or {}).items()):
                ucis = [m.get("uci") for m in (game.get("moves") or []) if m.get("uci")]
                have = games.get(gid)
                if have is None or have["uci"] != ucis[: len(have["uci"])]:
                    board = chess.Board()   # new game, or the history changed: start over
                    have = {"uci": [], "fens": [board.fen()], "board": board}
                    games[gid] = have
                for uci in ucis[len(have["uci"]):]:   # only the new moves
                    try:
                        have["board"].push_uci(uci)
                    except ValueError:
                        break
                    have["uci"].append(uci)
                    have["fens"].append(have["board"].fen())
                have["live"] = game.get("status") == "live"
                have["order"] = order
        self.wake.set()

    def annotations(self, state_path: Path) -> dict[str, dict[str, str]]:
        with self.lock:
            known = self.positions.get(Path(state_path)) or {}
            games = self.games.get(Path(state_path)) or {}
            return {gid: annotate_plies(g["uci"], [known.get(f) for f in g["fens"]]) for gid, g in games.items()}

    def known(self, state_path: Path) -> dict:
        """Every analysed position so far: {fen: {"cp", "second", "best", "over"}} (a copy)."""
        with self.lock:
            return dict(self.positions.get(Path(state_path)) or {})

    def progress(self, state_path: Path) -> dict:
        with self.lock:
            known = self.positions.get(Path(state_path)) or {}
            fens = {f for g in (self.games.get(Path(state_path)) or {}).values() for f in g["fens"]}
            return {"done": sum(1 for f in fens if f in known), "total": len(fens), "depth": self.depth, "error": self.error}

    # -- worker -----------------------------------------------------------------------------
    def _next(self) -> tuple[Path, str] | None:
        with self.lock:
            for state_path, games in self.games.items():
                known = self.positions.get(state_path) or {}
                # Live games first (newest moves are what viewers look at), then the rest in order.
                for game in sorted(games.values(), key=lambda g: (not g.get("live"), g.get("order", 0))):
                    for fen in game["fens"]:
                        if fen not in known:
                            return state_path, fen
        return None

    def _flush(self, force: bool = False) -> None:
        for state_path, count in list(self.dirty.items()):
            if count and (force or count >= 20):
                self.save(state_path)

    def _loop(self) -> None:
        import chess
        import chess.engine

        try:
            if self.shared is not None:
                engine = self.shared
            else:
                engine = chess.engine.SimpleEngine.popen_uci(str(self.engine_path))
                engine.configure({"Threads": self.threads, "Hash": self.hash_mb})
            self.name = engine.id.get("name", "Stockfish")
        except Exception as exc:
            self.error = f"annotator engine failed: {exc}"
            print(self.error, flush=True)
            return
        while True:
            job = self._next()
            if job is None:
                self._flush(force=True)
                self.wake.wait(2.0)
                self.wake.clear()
                continue
            state_path, fen = job
            try:
                record = analyse_position(engine, chess.Board(fen), self.depth)
            except Exception as exc:  # engine crash: say so and stop marking moves
                self.error = f"annotator stopped: {exc}"
                print(self.error, flush=True)
                self._flush(force=True)
                return
            with self.lock:
                self.positions.setdefault(state_path, {})[fen] = record
                self.dirty[state_path] = self.dirty.get(state_path, 0) + 1
            self._flush()


def analyse_position(engine, board, depth: int = NAG_DEPTH) -> dict:
    """{"cp", "second", "best", "over"}: scores from the side to move's point of view."""
    import chess.engine

    if board.is_game_over(claim_draw=False):
        return {"cp": -MATE_CP if board.is_checkmate() else 0, "second": None, "best": None, "over": True}
    infos = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=NAG_MULTIPV)
    if isinstance(infos, dict):
        infos = [infos]
    lines = []
    for info in infos:
        score = info.get("score")
        if score is None:
            continue
        rel = score.relative
        pv = info.get("pv") or []
        lines.append((score_cp(rel.score(), rel.mate()), pv[0].uci() if pv else None))
    if not lines:
        return {"cp": 0, "second": None, "best": None, "over": False}
    return {"cp": lines[0][0], "second": lines[1][0] if len(lines) > 1 else None, "best": lines[0][1], "over": False}


# ---- commentary (backend: tools/llm_commentary.py, written separately) -----------------------
AUDIO_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg"}


def start_commentator(state_path: Path | None, log=print, follow_dir: Path | None = None):
    """Commentator for the state file, or None when the module or the state is missing.

    follow_dir (the viewer runs without --state): the commentator follows the newest tournament file there,
    so a new tournament the runner starts gets its own commentary (memory reset and a fresh intro)."""
    if state_path is None:
        log("commentary off: no tournament state file to follow")
        return None
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    try:
        from llm_commentary import Commentator  # noqa: PLC0415 - optional, lazy
    except Exception as exc:
        log(f"commentary off: cannot import llm_commentary ({exc})")
        return None
    state_path = Path(state_path)
    try:
        commentator = Commentator(state_path=state_path, out_dir=state_path.parent / f"{state_slug(state_path)}-commentary",
                                  log=log, follow_dir=follow_dir)
        if commentator.start() is False:
            return None
    except Exception as exc:
        log(f"commentary off: Commentator failed to start ({exc})")
        return None
    log(f"commentary: on ({state_path.name})")
    return commentator


def safe_audio_name(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name and ":" not in name and not name.startswith(".")


# ---- live thinking text (written by the runner next to the state file) ----------------------
THINK_CHUNK = 60_000
THINK_MAX_PLY = 1000
_SAFE_ID = re.compile(r"^[A-Za-z0-9-]{1,80}$")
_state_ids: dict[Path, str] = {}


def safe_id(text: str) -> bool:
    return bool(_SAFE_ID.match(text or ""))


def state_id(state_path: Path) -> str:
    """The state JSON's "id" (read once per file), falling back to its file name."""
    state_path = Path(state_path)
    if state_path not in _state_ids:
        try:
            sid = str(json.loads(state_path.read_bytes()).get("id") or "")
        except (OSError, ValueError):
            sid = ""
        if not safe_id(sid):
            return state_slug(state_path)   # unreadable mid-write: try again next time
        _state_ids[state_path] = sid
    return _state_ids[state_path]


def thinking_path(state_path: Path, game: str, ply: int) -> Path:
    return Path(state_path).parent / f"{state_id(state_path)}-{game}-ply{ply}.thinking.txt"


def read_thinking(path: Path, since: int = 0, limit: int = THINK_CHUNK) -> dict:
    """New text of a thinking file from byte offset `since`.

    Never more than `limit` bytes: the first read (since 0) and a reader that fell far
    behind get only the tail (truncated). "size" is the offset to pass as the next `since`;
    a multi-byte character cut by a live writer is left for the next read.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, min(since, size))
            if since > size:            # the file was replaced: start over
                start = 0
            truncated = size - start > limit
            if truncated:
                start = size - limit
            handle.seek(start)
            raw = handle.read(size - start)
    except FileNotFoundError:
        return {"exists": False, "size": 0, "text": "", "from": 0, "truncated": False}
    if truncated:                       # do not start inside a multi-byte character
        skip = 0
        while skip < min(3, len(raw)) and 0x80 <= raw[skip] <= 0xBF:
            skip += 1
        raw, start = raw[skip:], start + skip
    end = len(raw)
    for back in range(1, min(4, len(raw)) + 1):   # drop an unfinished trailing character
        byte = raw[-back]
        if byte < 0x80:
            break
        if byte >= 0xC0:
            need = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            if back < need:
                end = len(raw) - back
            break
    raw = raw[:end]
    return {"exists": True, "size": start + len(raw), "text": raw.decode("utf-8", errors="replace"), "from": start, "truncated": truncated}


def fen_after(game: dict, ply: int) -> str:
    import chess

    board = chess.Board()
    for move in (game.get("moves") or [])[:ply]:
        board.push_uci(move["uci"])
    return board.fen()


def newest_state(live_dir: Path) -> Path | None:
    files = sorted(live_dir.glob("*-tournament.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


PUBLIC_PAGE_URL = "https://marvijo.com/ai-chess"
PUBLISH_FIELDS = ("ok", "relay_up", "last_state_push_epoch_ms", "clips_pushed", "error")


def publish_status(live_dir: Path, state_path: Path, now: float | None = None) -> dict | None:
    """The laptop pusher's report (<slug>-publish-status.json) for the local page's "Published" chip.

    No file (nothing is being published) = None, so the page shows no chip. Only the known fields
    pass, plus push_age_s (seconds since the last state push) and the public page URL.
    """
    path = Path(live_dir) / f"{state_slug(state_path)}-publish-status.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "relay_up": False, "error": "publish status file unreadable", "url": PUBLIC_PAGE_URL}
    if not isinstance(raw, dict):
        raw = {}
    out = {key: raw.get(key) for key in PUBLISH_FIELDS if key in raw}
    if out.get("error") is not None:
        out["error"] = str(out["error"])[:300]
    pushed = raw.get("last_state_push_epoch_ms")
    if isinstance(pushed, (int, float)) and pushed > 0:
        out["push_age_s"] = round((time.time() if now is None else now) - pushed / 1000, 1)
    out["url"] = PUBLIC_PAGE_URL
    return out


def pointer_state(pointer: Path, cache: dict | None = None) -> Path | None:
    """The state file a pointer JSON names (relative paths are relative to the pointer). Cached by mtime."""
    pointer = Path(pointer)
    try:
        mtime = pointer.stat().st_mtime_ns
    except OSError:
        return (cache or {}).get("path")
    if cache is not None and cache.get("mtime") == mtime:
        return cache.get("path")
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        path = Path(str(data["state_path"]))
    except (OSError, ValueError, KeyError, TypeError):
        return (cache or {}).get("path")   # mid-write: keep the last good answer
    if not path.is_absolute():
        path = pointer.parent / path
    if cache is not None:
        cache.update(mtime=mtime, path=path)
    return path


class Handler(BaseHTTPRequestHandler):
    state_path: Path | None = None
    follow: Path | None = None
    stream_layout = False
    _follow_cache: dict = {}
    live_dir: Path = LIVE_DIR
    analyzer: Analyzer | None = None
    annotator: Annotator | None = None
    commentator = None

    @classmethod
    def current_state(cls) -> Path | None:
        """The state to serve: the --follow pointer's target, else --state, else the newest state file."""
        if cls.follow is not None:
            return pointer_state(cls.follow, cls._follow_cache)
        return cls.state_path or newest_state(cls.live_dir)

    def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(min(length, 65536))
        if url.path == "/api/commentary/focus":
            self._commentary_focus(url)
        elif url.path == "/api/commentary/tour":
            on = (parse_qs(url.query).get("on") or ["0"])[0] in {"1", "true", "yes"}
            tour = getattr(self.commentator, "tour", None) if self.commentator is not None else None
            if tour is None:
                self._json({"enabled": False})
                return
            try:
                tour(on)
            except Exception as exc:
                self._json({"enabled": True, "error": str(exc)})
                return
            self._json({"enabled": True, "tour": on})
        else:
            self._send(404, b"not found", "text/plain")

    def _commentary_focus(self, url) -> None:
        game = (parse_qs(url.query).get("game") or [""])[0]
        if self.commentator is None:
            self._json({"enabled": False})
            return
        if game and not safe_id(game):
            self._json({"enabled": True, "error": "bad game id"}, 400)
            return
        try:
            # A game pins the commentary to that board; no game hands the choice back (auto).
            if game:
                self.commentator.focus(game, pinned=True)
            else:
                self.commentator.focus(None)
        except Exception as exc:
            self._json({"enabled": True, "game": game, "error": str(exc)})
            return
        self._json({"enabled": True, "game": game})

    def _thinking(self, query: dict) -> None:
        """?game=<id>&ply=<n>&since=<bytes>[&id=<tournament>]: the thinking text for one move."""
        game = (query.get("game") or [""])[0]
        wanted = (query.get("id") or [""])[0]
        try:
            ply = int((query.get("ply") or [""])[0])
            since = int((query.get("since") or ["0"])[0] or 0)
        except ValueError:
            self._json({"error": "ply and since must be whole numbers"}, 400)
            return
        if not safe_id(game) or (wanted and not safe_id(wanted)) or not 1 <= ply <= THINK_MAX_PLY or since < 0:
            self._json({"error": "bad game, id, ply or since"}, 400)
            return
        path = self.live_dir / f"{wanted}-tournament.json" if wanted else self.current_state()
        if not path or not path.exists():
            self._json({"error": "no tournament state yet"}, 404)
            return
        self._json(read_thinking(thinking_path(path, game, ply), since))

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path in {"/", "/index.html"}:
            page = PAGE.replace("<script>", "<script>window.AICHESS_STREAM = true;</script>\n<script>", 1) if self.stream_layout else PAGE
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif url.path == "/api/tournament":
            wanted = (parse_qs(url.query).get("id") or [""])[0]
            path = self.live_dir / f"{wanted}-tournament.json" if wanted else self.current_state()
            if not path or not path.exists():
                self._send(404, b'{"error":"no tournament state yet"}', "application/json")
                return
            for _ in range(5):
                try:
                    body = path.read_bytes()
                    json.loads(body)
                    break
                except (OSError, ValueError):
                    time.sleep(0.05)
            else:
                self._send(503, b'{"error":"state file busy"}', "application/json")
                return
            state = json.loads(body)
            now = time.time()
            state["state_age_s"] = round(now - (state.get("updated_epoch_ms") or 0) / 1000, 1)   # paused = old
            state["server_now_ms"] = int(now * 1000)
            if self.analyzer is not None:
                live = {gid: g.get("fen") for gid, g in (state.get("games") or {}).items()
                        if g.get("status") == "live" and g.get("fen")}
                self.analyzer.want(live=list(live.values()))
                state["analysis_engine"] = self.analyzer.name
                state["analysis"] = {gid: self.analyzer.get(fen) for gid, fen in live.items()}
            if self.annotator is not None:
                self.annotator.watch(path, state)
                state["annotation_engine"] = self.annotator.name
                state["annotations"] = self.annotator.annotations(path)
                state["annotation_progress"] = self.annotator.progress(path)
            else:
                state["annotations"] = {}
            state["commentary"] = self.commentator is not None
            if self.follow is not None:
                state["viewer_follow"] = True
            publish = publish_status(self.live_dir, path, now)
            if publish is not None:
                state["publish"] = publish
            self._json(state)
        elif url.path == "/api/analyze":
            # Replay position: ?game=<id>&ply=<n>. Answers from cache and queues deeper work.
            query = parse_qs(url.query)
            if self.analyzer is None:
                self._send(200, b'{"enabled":false}', "application/json")
                return
            path = self.current_state()
            try:
                state = json.loads(path.read_bytes()) if path else {}
                game = state["games"][query["game"][0]]
                fen = fen_after(game, int(query["ply"][0]))
            except Exception as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                return
            self.analyzer.want(extra=fen)
            payload = self.analyzer.get(fen) or {"fen": fen, "pending": True, "engine": self.analyzer.name}
            self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
        elif url.path == "/api/commentary":
            query = parse_qs(url.query)
            if self.commentator is None:
                self._json({"enabled": False, "clips": []})
                return
            game = (query.get("game") or [""])[0]
            try:
                after = int((query.get("after") or ["0"])[0])
            except ValueError:
                after = 0
            try:
                # No game: clips from every board in seq order (the commentator picks the board).
                clips = list((self.commentator.clips(game, after) if game else self.commentator.clips_all(after)) or [])
            except Exception as exc:
                self._json({"enabled": True, "clips": [], "error": str(exc)})
                return
            quiet = getattr(self.commentator, "quiet_s", None)
            held = getattr(self.commentator, "held", None)
            self._json({"enabled": True, "clips": clips, "quiet_s": quiet() if quiet else 0, "held": bool(held()) if held else False})
        elif url.path.startswith("/api/commentary/audio/"):
            name = unquote(url.path[len("/api/commentary/audio/"):])
            if self.commentator is None or not safe_audio_name(name):
                self._send(404, b"not found", "text/plain")
                return
            try:
                audio = self.commentator.audio_path(name)
            except Exception:
                audio = None
            audio = Path(audio) if audio else None
            kind = AUDIO_TYPES.get(audio.suffix.lower()) if audio else None
            if audio is None or kind is None or not audio.is_file():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, audio.read_bytes(), kind)
        elif url.path == "/api/commentary/focus":
            self._commentary_focus(url)
        elif url.path == "/api/thinking":
            self._thinking(parse_qs(url.query))
        elif url.path == "/api/viewer-version":
            self._send(200, json.dumps({"version": VIEWER_VERSION}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--live-dir", type=Path, default=LIVE_DIR)
    parser.add_argument("--engine", type=Path, default=DEFAULT_ENGINE, help="UCI engine for viewer-only analysis")
    parser.add_argument("--no-analysis", action="store_true", help="no Stockfish eval bar and no move marks")
    parser.add_argument("--no-annotations", action="store_true", help="no ?? ? ?! ! move marks")
    parser.add_argument("--annotation-depth", type=int, default=NAG_DEPTH)
    # RAM on a small server (env defaults so a service file can set them): one shared Stockfish, small hash.
    parser.add_argument("--one-engine", action="store_true", default=os.environ.get("AICHESS_ONE_ENGINE", "") == "1",
                        help="one Stockfish process for the eval bar and the move marks (env AICHESS_ONE_ENGINE=1)")
    parser.add_argument("--engine-hash", type=int, default=int(os.environ.get("AICHESS_ENGINE_HASH_MB") or 0) or None,
                        help="Stockfish hash in MB for the viewer's analysis (env AICHESS_ENGINE_HASH_MB; default 512 eval + 128 marks)")
    parser.add_argument("--engine-threads", type=int, default=int(os.environ.get("AICHESS_ENGINE_THREADS") or 0) or None,
                        help="Stockfish threads for the viewer's analysis (env AICHESS_ENGINE_THREADS; default 4 eval + 2 marks)")
    parser.add_argument("--commentary", action="store_true", help="serve tools/llm_commentary.py clips")
    parser.add_argument("--follow", type=Path, help="pointer JSON {state_path, id, number}: always serve the tournament it names")
    parser.add_argument("--stream-layout", action="store_true", help="serve the 1920x1080 stream layout at / (same as ?stream=1)")
    args = parser.parse_args(argv)
    Handler.state_path = args.state.resolve() if args.state else None
    Handler.follow = args.follow.resolve() if args.follow else None
    Handler.stream_layout = bool(args.stream_layout)
    Handler.live_dir = args.live_dir.resolve()
    if Handler.follow is not None and "--live-dir" not in (argv if argv is not None else sys.argv[1:]):
        Handler.live_dir = Handler.follow.parent   # the pointer sits in the live dir next to the state files
    if not args.no_analysis:
        if args.engine.exists():
            shared = None
            if args.one_engine:
                shared = SharedEngine(args.engine, threads=args.engine_threads or 1, hash_mb=args.engine_hash or 64)
                print(f"analysis: one shared engine, Threads {args.engine_threads or 1}, Hash {args.engine_hash or 64} MB", flush=True)
            Handler.analyzer = Analyzer(args.engine, threads=args.engine_threads or 4, hash_mb=args.engine_hash or 512, shared=shared)
            print(f"analysis: {Handler.analyzer.name} ({args.engine})", flush=True)
            if not args.no_annotations:
                Handler.annotator = Annotator(args.engine, depth=args.annotation_depth, threads=args.engine_threads or 2,
                                              hash_mb=args.engine_hash or 128, shared=shared).start()
                print(f"move marks: depth {args.annotation_depth}, MultiPV {NAG_MULTIPV}", flush=True)
        else:
            print(f"analysis off: engine not found at {args.engine}", flush=True)
    if args.commentary:
        # One commentator for the life of the viewer. Without --state (and with --follow) it follows the newest
        # tournament file in the live dir: the runner writes the pointer's state every few seconds, so that is
        # the tournament the pointer names. It switches itself (memory reset, fresh intro) and keeps counting
        # clip numbers across tournaments, so the relay never sees a clip number twice.
        def attach_commentary() -> None:
            # The viewer can start before the runner wrote its first state (services start in any order):
            # wait for it instead of leaving the commentary off for good.
            start_path = Handler.current_state()
            while start_path is None or not Path(start_path).exists():
                time.sleep(5)
                start_path = Handler.current_state()
            Handler.commentator = start_commentator(start_path,
                                                    follow_dir=None if Handler.state_path else Handler.live_dir)
            if Handler.commentator is not None and Handler.annotator is not None:
                # The commentator's own state_path: it moves to the next tournament file when the runner starts one.
                Handler.commentator.marks_provider = lambda: Handler.annotator.annotations(Handler.commentator.state_path)
                Handler.commentator.positions_provider = lambda: Handler.annotator.known(Handler.commentator.state_path)

        first = Handler.current_state()
        if first is not None and Path(first).exists():
            attach_commentary()
        else:
            print("commentary: waiting for the first tournament state", flush=True)
            threading.Thread(target=attach_commentary, daemon=True).start()
    if Handler.follow is not None:
        print(f"follow: {Handler.follow} -> {Handler.current_state()}", flush=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"tournament viewer on http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
