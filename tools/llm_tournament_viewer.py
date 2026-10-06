"""Live web view of an LLM Swiss tournament written by tools/play_llm_swiss.py.

    python tools/llm_tournament_viewer.py --port 8770 [--state out/live/<slug>-tournament.json] [--commentary]

Without --state it follows the newest out/live/*-tournament.json. The page polls
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
/* The whole game card (bars, board, comment, moves) must fit one 1080p screen. */
.boards > .card[data-game] { width: 100%; max-width: max(360px, calc(100vh - 535px)); justify-self: center; }
.card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin: 0 0 10px; font-weight: 600; }
.game-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; gap: 6px 8px; cursor: pointer; min-width: 0; flex-wrap: wrap; }
.card.focused .game-head { cursor: default; }
.game-head .tag { font-size: 12px; color: var(--muted); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.game-head .head-right { display: flex; gap: 6px; align-items: center; flex: none; }
.result-pill { font-weight: 700; font-size: 13px; padding: 2px 10px; border-radius: 999px; background: var(--panel-2); border: 1px solid var(--line); white-space: nowrap; }
.result-pill.live { color: var(--ok); border-color: rgba(52, 199, 123, .45); }
.pbar { display: flex; align-items: center; gap: 10px; padding: 7px 10px; border-radius: 10px; background: var(--panel-2); margin: 6px 0; min-width: 0; }
.pbar .dot { width: 14px; height: 14px; border-radius: 50%; flex: none; border: 1px solid #666; }
.pbar .dot.w { background: #fff; } .pbar .dot.b { background: #111; }
.pbar .name { font-weight: 600; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
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
.caption::before { content: "Commentary: "; color: var(--accent); font-weight: 600; }
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
.pair .w { text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pair .b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pair .r { text-align: center; font-weight: 700; }
.pair .r.live { color: var(--ok); font-size: 12px; }
.pair .why { grid-column: 1 / -1; color: var(--muted); font-size: 11.5px; text-align: center; margin-top: -2px; }
.bye { font-size: 12.5px; color: var(--muted); padding: 2px 8px; }
.empty { color: var(--muted); padding: 30px; text-align: center; }
.rules { color: var(--muted); font-size: 13px; line-height: 1.55; margin: 0; padding-left: 18px; }
/* Focus mode: one board, as large as the viewport allows, with its info column beside it. */
body.focus-mode main { grid-template-columns: minmax(0, 1fr); }
body.focus-mode .side { display: none; }
body.focus-mode .boards { display: block; }
.boards > .card.focused { max-width: none; display: grid; gap: 4px 22px; align-items: start;
  --fboard: max(340px, min(calc(100vh - 290px), calc(100vw - 560px)));
  grid-template-columns: var(--fboard) minmax(300px, 1fr); grid-template-areas: "head head" "boardcol infocol"; }
.card.focused .game-head { grid-area: head; }
.card.focused .boardcol { grid-area: boardcol; min-width: 0; }
.card.focused .infocol { grid-area: infocol; min-width: 0; display: flex; flex-direction: column; height: calc(var(--fboard) + 64px); }
.card.focused .infocol .evalline { margin-top: 6px; }
.card.focused .moves { max-height: none; flex: 1 1 auto; min-height: 140px; font-size: 14px; }
@media (max-width: 1100px) { main { grid-template-columns: 1fr; } }
@media (max-width: 900px) {
  .boards > .card.focused { grid-template-columns: minmax(0, 1fr); grid-template-areas: "head" "boardcol" "infocol"; }
  .card.focused .infocol { height: auto; }
  .card.focused .moves { flex: none; max-height: 240px; min-height: 0; }
}
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
<main>
  <section class="boards" id="boards"><div class="empty card">Waiting for the tournament to start...</div></section>
  <aside class="side">
    <div class="card"><h2>Standings (Elo)</h2><div id="standings"></div></div>
    <div class="card"><h2>Rounds</h2><div id="rounds"></div></div>
    <div class="card"><h2>Rules</h2><ul class="rules" id="rules"></ul></div>
  </aside>
</main>
<script>
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
let commentaryOn = store("swissCommentary") === "on";
const replayEval = {};        // "game|ply" -> Stockfish result for replay positions
let replayFetchAt = 0;
const cards = new Map();      // game id -> persistent card DOM + render keys

function readHash() {
  const m = location.hash.match(/focus=([^&]+)/);
  focusId = m ? decodeURIComponent(m[1]) : null;
}
readHash();
function setFocus(id) {
  if (id === focusId) return;
  focusId = id;
  const url = id ? `#focus=${encodeURIComponent(id)}` : location.pathname + location.search;
  try { history.pushState(null, "", url); } catch (e) { location.hash = id ? `focus=${encodeURIComponent(id)}` : ""; }
  if (id) window.scrollTo(0, 0);
  render();
}
window.addEventListener("popstate", () => { readHash(); render(); });
window.addEventListener("hashchange", () => { readHash(); render(); });

function evalText(a) {
  if (!a) return "...";
  if (a.over) return "game over";
  if (a.mate !== null && a.mate !== undefined) return (a.mate > 0 ? "+M" : "-M") + Math.abs(a.mate);
  if (a.cp === null || a.cp === undefined) return "...";
  return (a.cp > 0 ? "+" : "") + (a.cp / 100).toFixed(2);
}
function whiteShare(a) {
  if (!a || a.over) return 50;
  if (a.mate !== null && a.mate !== undefined) return a.mate > 0 ? 100 : 0;
  if (a.cp === null || a.cp === undefined) return 50;
  return 100 / (1 + Math.exp(-a.cp / 250));
}
function evalFor(game, ply, total) {
  if (!analysisOn) return null;
  if (ply >= total && game.status === "live") return (data.analysis || {})[game.id] || null;
  const key = `${game.id}|${ply}`;
  const have = replayEval[key];
  if ((!have || ((have.depth || 0) < 20 && !have.over)) && Date.now() - replayFetchAt > 700) {
    replayFetchAt = Date.now();
    fetch(`/api/analyze?game=${encodeURIComponent(game.id)}&ply=${ply}`, { cache: "no-store" })
      .then(r => r.ok ? r.json() : null).then(j => { if (j && !j.pending) replayEval[key] = j; }).catch(() => {});
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
      <div class="pbar" data-part="white"></div><div class="caption" data-part="caption" style="display:none"></div></div>
    <div class="infocol"><div class="evalline" data-part="evalline"></div><div class="comment" data-part="comment"></div>
      <div class="comment" data-part="result" style="display:none"></div><div class="moves" data-part="moves"></div><div class="nav" data-part="nav"></div></div>`;
  const parts = { head: el.querySelector("[data-focus-head]") };
  el.querySelectorAll("[data-part]").forEach(n => { parts[n.dataset.part] = n; });
  c = { id, el, parts, follow: true, top: 0, movesKey: null, boardKey: null, moveTotal: undefined, movePly: undefined };
  parts.moves.addEventListener("scroll", () => {
    const box = parts.moves;
    c.top = box.scrollTop;
    c.follow = box.scrollHeight - box.scrollTop - box.clientHeight < 8;
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
  const thinking = ticking ? `thinking ${clock(think)}` : (moving ? "starting..." : "");
  el.classList.toggle("to-move", moving);
  setHTML(el, `<span class="dot ${side[0]}"></span><span class="name" title="${esc(route(name))}">${esc(name)}</span>`
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
  const pill = live ? `<span class="result-pill live">LIVE - move ${Math.floor(total / 2) + 1}</span>`
    : `<span class="result-pill">${esc(game.result || "*")}</span>`;
  const btn = focused ? `<button class="linkbtn small" data-unfocus title="Back to all boards (Esc)">Back to all boards</button>`
    : `<button class="linkbtn small" data-focus="${esc(game.id)}" title="Show only this board, large">Focus</button>`;
  setHTML(p.head, `<span class="tag">Round ${game.round} - Board ${game.board}${pinned && ply < total ? " - replay" : ""}</span><span class="head-right">${pill}${btn}</span>`);
  updateBar(p.black, game, "black", now);
  updateBar(p.white, game, "white", now);
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
    setHTML(p.evalline, `<span class="score">${evalText(ev)}</span><span class="pv">${ev && ev.best ? "best " + esc(ev.best) + " - " + esc(ev.pv) : (ev && ev.over ? "" : "analysing...")}</span><span class="eng">${esc((ev && ev.engine) || data.analysis_engine || "Stockfish")}${ev && ev.depth && ev.depth < 99 ? " d" + ev.depth : ""}</span>`);
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
  setHTML(p.caption, esc(cap));
  updateMoves(c, game, ply, pinned, ann);
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
    for (const g of shown) { const c = cardFor(g.id); c.movesKey = null; c.moveTotal = undefined; }
  }
  const now = Date.now();
  for (const g of shown) updateCard(cardFor(g.id), g, now);
}

function render() {
  if (!data || !data.id) return;
  document.getElementById("title").textContent = data.title || "AI Chess Swiss";
  document.title = (focusId && data.games && data.games[focusId] ? `${data.games[focusId].white} vs ${data.games[focusId].black} - ` : "") + (data.title || "AI Chess Swiss");
  const cfg = data.config || {};
  const finished = data.finished;
  setHTML(document.getElementById("chips"), [
    finished ? `<span class="chip done">Finished - winner <b>${esc(data.winner || "")}</b></span>` : `<span class="chip live">Round <b>${data.current_round}</b> of ${cfg.rounds}</span>`,
    data.paused ? `<span class="chip" title="${esc(data.paused)}">Paused: a provider is unavailable; the game will be replayed</span>` : "",
    `<span class="chip"><b>${Math.round((cfg.timeControlMs || 0) / 60000)} min${cfg.incrementMs ? " + " + Math.round(cfg.incrementMs / 1000) + " s" : ""}</b> per player</span>`,
    `<span class="chip"><b>${cfg.maxAttempts}</b> tries per move, then forfeit</span>`,
    `<span class="chip">Swiss, Elo start <b>${cfg.startElo}</b>, K=${cfg.eloK}</span>`,
    data.analysis_engine ? `<span class="chip btn ${analysisOn ? "on" : ""}" data-toggle-analysis title="Viewer-only engine analysis; the AI players never see it">${esc(data.analysis_engine)} analysis: <b>${analysisOn ? "on" : "off"}</b></span>` : "",
    `<span class="chip btn ${soundOn ? "on" : ""}" data-toggle-sound title="A short click whenever a new move appears on a visible board">Move sound: <b>${soundOn ? "on" : "off"}</b></span>`,
    data.commentary ? `<span class="chip btn ${commentaryOn ? (needGesture ? "wait" : "on") : ""}" data-toggle-commentary title="Spoken commentary for the focused (or first live) board">Commentary: <b>${commentaryOn ? (needGesture ? "click to start" : "on") : "muted"}</b></span>` : "",
  ].join(""));
  renderBoards();
  const games = data.games || {};
  const rows = (data.standings || []).map(r => {
    const d = r.elo_delta || 0;
    return `<tr class="${r.rank === 1 && (r.played || 0) > 0 ? "rank1" : ""}"><td>${r.rank}</td><td class="player">${esc(r.name)}<span class="route">${esc(route(r.name))}</span></td>`
      + `<td class="num"><b>${r.points}</b></td><td class="num">${Math.round(r.elo)} <span class="${d > 0 ? "up" : d < 0 ? "down" : ""}">${Math.round(d) ? (d > 0 ? "+" : "") + Math.round(d) : ""}</span></td>`
      + `<td class="num">${r.wins}/${r.draws}/${r.losses}</td><td class="num">${r.forfeits}</td><td class="num">${r.flags}</td><td class="num">${r.invalid_attempts}</td></tr>`;
  }).join("");
  setHTML(document.getElementById("standings"), `<table><thead><tr><th>#</th><th>Player</th><th class="num">Pts</th><th class="num">Elo</th><th class="num">W/D/L</th><th class="num" title="Lost by 3 invalid replies">Forf</th><th class="num" title="Lost on time">Flag</th><th class="num" title="Rejected replies">Bad</th></tr></thead><tbody>${rows}</tbody></table>`);
  setHTML(document.getElementById("rounds"), (data.rounds || []).slice().reverse().map(r => {
    const pairs = r.pairings.map(p => {
      const g = games[p.game_id] || {};
      const res = g.status === "live" ? `<span class="r live">LIVE</span>` : `<span class="r">${esc(g.result && g.result !== "*" ? g.result.replace("1/2-1/2", "½-½") : "-")}</span>`;
      return `<div class="pair ${selected === p.game_id ? "sel" : ""}" data-pick="${esc(p.game_id)}"><span class="w">${esc(p.white)}</span>${res}<span class="b">${esc(p.black)}</span>`
        + (g.termination ? `<span class="why">${esc(g.termination)}</span>` : "") + `</div>`;
    }).join("");
    return `<div class="round"><div class="round-title"><span>Round ${r.round}</span><span>${r.status === "finished" ? "done" : "playing"}</span></div>${pairs}${r.bye ? `<div class="bye">Bye: ${esc(r.bye)} (sits out this round${cfg.byePoints ? `, +${cfg.byePoints}` : ", no points"})</div>` : ""}</div>`;
  }).join("") || `<div class="empty">No rounds yet</div>`);
  setHTML(document.getElementById("rules"), [
    "Each AI picks every move itself: no tools, no code, no chess engine.",
    `${cfg.maxAttempts} replies per move; an illegal or broken reply is rejected with the reason, the third one forfeits the game.`,
    `${Math.round((cfg.timeControlMs || 0) / 60000)} minutes of model thinking time per player, ${cfg.incrementMs ? "+" + Math.round(cfg.incrementMs / 1000) + " s per move" : "no increment"}; the clock runs out = loss on time.`,
    "Every model thinks at High effort. Past the move cap (1.5x its time budget) its thinking stops, it gets all of that thinking back and gives its move. Running out of time is never an invalid reply.",
    "Points: 1 for a win, 0.5 for a draw, 0 for a loss or a bye.",
    "Swiss pairing: same score meets same score, no rematches, one bye each.",
    "Stockfish analysis is for viewers only: the AI players never see it.",
    data.annotation_engine ? "Move marks (?? ? ?! !) come from Stockfish 19 for viewers only." : "",
  ].filter(Boolean).map(x => `<li>${esc(x)}</li>`).join(""));
  syncCommentaryTarget();
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
let caption = null;           // { game, text } shown under that board
let needGesture = false;      // the browser blocked play(): wait for a click
const commentaryQueue = [];
const lastSeq = {};
let clipPlaying = false;
let clipTimer = null;
let clipToken = 0;            // bumped by stopClip so a stopped clip's late callbacks are ignored
let commentaryTarget = null;
let focusSent = null;
const clipAudio = new Audio();
clipAudio.preload = "auto";
function targetGame() {
  if (!data || !data.games) return null;
  if (focusId) return data.games[focusId] ? focusId : null;
  const live = shownGames().find(g => g.status === "live");
  return live ? live.id : null;
}
function stopClip() {
  clipToken++;
  clearTimeout(clipTimer);
  try { clipAudio.pause(); } catch (e) { /* ignore */ }
  clipPlaying = false;
  caption = null;
}
function syncCommentaryTarget() {
  if (!data || !data.commentary) return;
  const t = targetGame();
  if (t !== commentaryTarget) {
    commentaryTarget = t;
    commentaryQueue.length = 0;
    stopClip();
  }
  if (t && t !== focusSent) {
    focusSent = t;
    fetch(`/api/commentary/focus?game=${encodeURIComponent(t)}`, { method: "POST", cache: "no-store" }).catch(() => {});
  }
}
function clipDone() {
  clearTimeout(clipTimer);
  clipPlaying = false;
  caption = null;
  render();
  playNextClip();
}
function playNextClip() {
  if (clipPlaying || needGesture || !commentaryOn) return;
  while (commentaryQueue.length) {
    const clip = commentaryQueue.shift();
    const game = (data.games || {})[clip.game];
    const total = game ? (game.moves || []).length : 0;
    if (!game || clip.game !== commentaryTarget || total - (clip.ply || 0) > 2) continue;   // stale: 2+ moves behind
    clipPlaying = true;
    caption = { game: clip.game, text: clip.text || "" };
    render();
    if (clip.audio) {
      const token = clipToken;
      clipAudio.src = `/api/commentary/audio/${encodeURIComponent(clip.audio)}`;
      clipAudio.play().catch(err => {
        if (token !== clipToken) return;             // stopped on purpose (mute, other board)
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
  if (!data || !data.commentary || !commentaryOn || !commentaryTarget) return;
  const game = commentaryTarget;
  try {
    const res = await fetch(`/api/commentary?game=${encodeURIComponent(game)}&after=${lastSeq[game] || 0}`, { cache: "no-store" });
    if (!res.ok) return;
    const j = await res.json();
    for (const clip of (j.clips || [])) {
      if (typeof clip.seq !== "number" || clip.seq <= (lastSeq[game] || 0)) continue;
      lastSeq[game] = clip.seq;
      if (game === commentaryTarget) commentaryQueue.push(Object.assign({ game }, clip));
    }
    playNextClip();
  } catch (e) { /* try again next tick */ }
}
setInterval(pollCommentary, 2000);

// ---- input --------------------------------------------------------------------------------
document.addEventListener("click", ev => {
  unlockAudio();
  if (needGesture) { needGesture = false; playNextClip(); }
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

async function poll() {
  try {
    const q = params.get("id") ? `?id=${encodeURIComponent(params.get("id"))}` : "";
    const res = await fetch(`/api/tournament${q}`, { cache: "no-store" });
    if (res.ok) { data = await res.json(); soundForNewMoves(); render(); }
  } catch (e) { /* keep the last frame */ }
}
poll();
setInterval(poll, 1000);
setInterval(render, 500);   // clocks tick between polls; unchanged parts are not touched
</script>
</body>
</html>
"""


DEFAULT_ENGINE = ROOT / "out" / "engines" / "stockfish-19" / "stockfish" / "stockfish-windows-x86-64-universal.exe"


class Analyzer:
    """Viewer-only Stockfish analysis (never sent to the players).

    Live positions are searched in short slices, round robin, until they reach the target
    depth; Stockfish's hash makes every slice continue deeper. Replay positions asked for by
    the page join the queue behind the live ones.
    """

    def __init__(self, path: Path, slice_s: float = 0.8, target_depth: int = 26, threads: int = 4, hash_mb: int = 512) -> None:
        import chess.engine

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

    def __init__(self, path: Path, depth: int = NAG_DEPTH, threads: int = 2, hash_mb: int = 128) -> None:
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


def start_commentator(state_path: Path | None, log=print):
    """Commentator for the state file, or None when the module or the state is missing."""
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
        commentator = Commentator(state_path=state_path, out_dir=state_path.parent / f"{state_slug(state_path)}-commentary", log=log)
        commentator.start()
    except Exception as exc:
        log(f"commentary off: Commentator failed to start ({exc})")
        return None
    log(f"commentary: on ({state_path.name})")
    return commentator


def safe_audio_name(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name and ":" not in name and not name.startswith(".")


def fen_after(game: dict, ply: int) -> str:
    import chess

    board = chess.Board()
    for move in (game.get("moves") or [])[:ply]:
        board.push_uci(move["uci"])
    return board.fen()


def newest_state(live_dir: Path) -> Path | None:
    files = sorted(live_dir.glob("*-tournament.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


class Handler(BaseHTTPRequestHandler):
    state_path: Path | None = None
    live_dir: Path = LIVE_DIR
    analyzer: Analyzer | None = None
    annotator: Annotator | None = None
    commentator = None

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
        else:
            self._send(404, b"not found", "text/plain")

    def _commentary_focus(self, url) -> None:
        game = (parse_qs(url.query).get("game") or [""])[0]
        if self.commentator is None:
            self._json({"enabled": False})
            return
        try:
            self.commentator.focus(game or None)
        except Exception as exc:
            self._json({"enabled": True, "game": game, "error": str(exc)})
            return
        self._json({"enabled": True, "game": game})

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path in {"/", "/index.html"}:
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif url.path == "/api/tournament":
            wanted = (parse_qs(url.query).get("id") or [""])[0]
            path = self.live_dir / f"{wanted}-tournament.json" if wanted else (self.state_path or newest_state(self.live_dir))
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
            self._json(state)
        elif url.path == "/api/analyze":
            # Replay position: ?game=<id>&ply=<n>. Answers from cache and queues deeper work.
            query = parse_qs(url.query)
            if self.analyzer is None:
                self._send(200, b'{"enabled":false}', "application/json")
                return
            path = self.state_path or newest_state(self.live_dir)
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
                clips = list(self.commentator.clips(game, after) or [])
            except Exception as exc:
                self._json({"enabled": True, "clips": [], "error": str(exc)})
                return
            self._json({"enabled": True, "clips": clips})
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
    parser.add_argument("--commentary", action="store_true", help="serve tools/llm_commentary.py clips")
    args = parser.parse_args(argv)
    Handler.state_path = args.state.resolve() if args.state else None
    Handler.live_dir = args.live_dir.resolve()
    if not args.no_analysis:
        if args.engine.exists():
            Handler.analyzer = Analyzer(args.engine)
            print(f"analysis: {Handler.analyzer.name} ({args.engine})", flush=True)
            if not args.no_annotations:
                Handler.annotator = Annotator(args.engine, depth=args.annotation_depth).start()
                print(f"move marks: depth {args.annotation_depth}, MultiPV {NAG_MULTIPV}", flush=True)
        else:
            print(f"analysis off: engine not found at {args.engine}", flush=True)
    if args.commentary:
        Handler.commentator = start_commentator(Handler.state_path or newest_state(Handler.live_dir))
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"tournament viewer on http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
