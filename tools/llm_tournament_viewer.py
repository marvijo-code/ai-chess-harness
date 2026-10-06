"""Live web view of an LLM Swiss tournament written by tools/play_llm_swiss.py.

    python tools/llm_tournament_viewer.py --port 8770 [--state out/live/<slug>-tournament.json]

Without --state it follows the newest out/live/*-tournament.json. The page polls
/api/tournament once a second and shows every board of the current round, the Elo
standings and all rounds; click a finished game to replay it with the arrow keys.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
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
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.4 "Segoe UI", system-ui, -apple-system, sans-serif; }
body { min-height: 100vh; }
header { display: flex; align-items: center; gap: 16px; padding: 14px 22px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
h1 { font-size: 22px; margin: 0; letter-spacing: .2px; }
.chips { display: flex; gap: 8px; flex-wrap: wrap; }
.chip { background: var(--panel-2); border: 1px solid var(--line); border-radius: 999px; padding: 3px 11px; font-size: 13px; color: var(--muted); white-space: nowrap; }
.chip b { color: var(--text); font-weight: 600; }
.chip.live { color: var(--ok); border-color: rgba(52, 199, 123, .45); }
.chip.done { color: var(--warn); border-color: rgba(245, 185, 66, .45); }
main { display: grid; grid-template-columns: minmax(0, 1fr) 470px; gap: 18px; padding: 18px 22px; align-items: start; }
.boards { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 18px; min-width: 0; align-items: start; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 14px; min-width: 0; }
/* The whole game card (bars, board, comment, moves) must fit one 1080p screen. */
.boards > .card[data-game] { width: 100%; max-width: max(360px, calc(100vh - 430px)); justify-self: center; }
.card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin: 0 0 10px; font-weight: 600; }
.game-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; gap: 8px; }
.game-head .tag { font-size: 12px; color: var(--muted); }
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
.evalbar .white { position: absolute; left: 0; right: 0; bottom: 0; background: #f2f2f2; transition: height .6s ease; }
.evalbar .mid { position: absolute; left: 0; right: 0; top: 50%; height: 1px; background: rgba(255, 85, 85, .7); }
.evalline { margin-top: 8px; padding: 7px 10px; border-radius: 10px; background: #12161c; border: 1px solid var(--line); font-size: 13px; color: var(--muted); display: flex; gap: 10px; align-items: baseline; min-width: 0; }
.evalline .score { font: 700 16px/1 ui-monospace, "Cascadia Mono", Consolas, monospace; color: var(--text); min-width: 58px; }
.evalline .pv { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1; }
.evalline .eng { white-space: nowrap; }
.chip.btn { cursor: pointer; } .chip.btn.on { color: var(--accent); border-color: rgba(91, 157, 255, .5); }
.comment { margin-top: 10px; padding: 9px 11px; background: var(--panel-2); border-radius: 10px; font-size: 14px; min-height: 58px; }
.comment .who { color: var(--muted); font-size: 12px; margin-bottom: 2px; }
.moves { margin-top: 8px; font: 13px/1.6 ui-monospace, "Cascadia Mono", Consolas, monospace; color: var(--muted); max-height: 76px; overflow-y: auto; word-break: break-word; }
.moves .cur { color: var(--text); background: rgba(91, 157, 255, .25); border-radius: 4px; padding: 0 3px; }
.moves .bad { color: var(--bad); }
.nav { display: flex; gap: 6px; margin-top: 8px; align-items: center; flex-wrap: wrap; }
.nav button, .linkbtn { background: var(--panel-2); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 4px 10px; cursor: pointer; font: inherit; font-size: 13px; }
.nav button:hover, .linkbtn:hover { border-color: var(--accent); }
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
@media (max-width: 1100px) { main { grid-template-columns: 1fr; } }
@media (max-width: 520px) {
  main { padding: 12px 16px; } header { padding: 12px 16px; }
  .boards { grid-template-columns: 1fr; }
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
let data = null;
let selected = null;          // game id pinned by a click (replay); null = follow live boards
let replayPly = null;         // ply shown for the selected game (null = last)
const params = new URLSearchParams(location.search);
let analysisOn = true;
try { analysisOn = localStorage.getItem("swissAnalysis") !== "off"; } catch (e) { /* storage blocked */ }
const replayEval = {};        // "game|ply" -> Stockfish result for replay positions
let replayFetchAt = 0;

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
  if (ply >= total) return (data.analysis || {})[game.id] || null;
  const key = `${game.id}|${ply}`;
  const have = replayEval[key];
  if ((!have || (have.depth || 0) < 20) && Date.now() - replayFetchAt > 1200) {
    replayFetchAt = Date.now();
    fetch(`/api/analyze?game=${encodeURIComponent(game.id)}&ply=${ply}`, { cache: "no-store" })
      .then(r => r.ok ? r.json() : null).then(j => { if (j && !j.pending) replayEval[key] = j; }).catch(() => {});
  }
  return have || null;
}

function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function clock(ms) { ms = Math.max(0, Math.floor((ms || 0) / 1000)); const m = Math.floor(ms / 60), s = ms % 60; return `${m}:${String(s).padStart(2, "0")}`; }
function fenBoard(fen) {
  const rows = (fen || "8/8/8/8/8/8/8/8").split(" ")[0].split("/");
  return rows.map(r => { const out = []; for (const ch of r) { if (/\d/.test(ch)) for (let i = 0; i < +ch; i++) out.push(null); else out.push(ch); } return out; });
}
function fenAt(game, ply) {
  // Positions are rebuilt client side from SAN-free UCI moves.
  const moves = game.moves || [];
  if (ply === null || ply >= moves.length) return { fen: game.fen, last: moves.length ? moves[moves.length - 1].uci : null };
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
function standingRow(name) { return (data.standings || []).find(r => r.name === name) || {}; }
function route(name) { const p = (data.players || []).find(x => x.name === name); return p ? (p.route || p.provider) : ""; }

function gameCard(game) {
  const live = game.status === "live";
  const pinned = selected === game.id;
  const moves = game.moves || [];
  const ply = pinned && replayPly !== null ? Math.min(replayPly, moves.length) : moves.length;
  const pos = ply < moves.length ? fenAt(game, ply) : { squares: fenBoard(game.fen), last: moves.length ? moves[moves.length - 1].uci : null };
  const toMove = (game.fen || "").split(" ")[1] === "b" ? "black" : "white";
  const now = Date.now();
  const think = game.thinking && game.thinking.since_epoch_ms ? Math.max(0, now - game.thinking.since_epoch_ms) : 0;
  const bar = side => {
    const name = game[side];
    const s = standingRow(name);
    const moving = live && toMove === side;
    const ticking = moving && game.thinking && game.thinking.side === side;
    // The side to move's clock runs down live from the moment its model starts thinking.
    let clk = game.clocks ? game.clocks[side] : (data.config.timeControlMs || 0);
    if (ticking) clk = Math.max(0, clk - think);
    const thinking = ticking ? `thinking ${clock(think)}` : (moving ? "starting..." : "");
    return `<div class="pbar ${moving ? "to-move" : ""}"><span class="dot ${side[0]}"></span><span class="name" title="${esc(route(name))}">${esc(name)}</span>`
      + `<span class="elo">${s.elo ? Math.round(s.elo) : ""}</span><span class="think">${thinking}</span><span class="clock">${clock(clk)}</span></div>`;
  };
  const shownMove = ply > 0 ? moves[ply - 1] : null;
  // undefined = analysis panel hidden; null = waiting for the first result.
  const ev = analysisOn && data.analysis_engine ? evalFor(game, ply, moves.length) : undefined;
  const commentWho = shownMove ? `${esc(game[shownMove.side])} - ${Math.ceil(shownMove.ply / 2)}${shownMove.side === "white" ? "." : "..."} ${esc(shownMove.san)}`
    + (shownMove.tries > 1 ? ` (try ${shownMove.tries})` : "") + ` - ${(shownMove.elapsed_ms / 1000).toFixed(0)}s`
    + (shownMove.hurried ? ` - thinking stopped at the cap` : "") : "No moves yet";
  const moveText = moves.map((m, i) => {
    const num = m.side === "white" ? `${Math.ceil(m.ply / 2)}.` : "";
    const cls = (i + 1 === ply ? "cur" : "") + (m.tries > 1 ? " bad" : "");
    return `${num}<span class="${cls}" title="${m.tries > 1 ? "needed " + m.tries + " tries" : ""}">${esc(m.san)}</span>`;
  }).join(" ");
  const pill = live ? `<span class="result-pill live">LIVE - move ${Math.floor((moves.length) / 2) + 1}</span>`
    : `<span class="result-pill">${esc(game.result || "*")}</span>`;
  return `<div class="card" data-game="${esc(game.id)}">
    <div class="game-head"><span class="tag">Round ${game.round} - Board ${game.board}${pinned && !live ? " - replay" : ""}</span>${pill}</div>
    ${bar("black")}
    <div class="board-wrap ${ev === undefined ? "no-eval" : ""}">${ev === undefined ? "" : `<div class="evalbar" title="Stockfish evaluation, White at the bottom"><div class="white" style="height:${whiteShare(ev).toFixed(1)}%"></div><div class="mid"></div></div>`}<div class="board">${boardHtml(pos.squares, pos.last)}</div></div>
    ${bar("white")}
    ${ev === undefined ? "" : `<div class="evalline"><span class="score">${evalText(ev)}</span><span class="pv">${ev && ev.best ? "best " + esc(ev.best) + " - " + esc(ev.pv) : (ev && ev.over ? "" : "analysing...")}</span><span class="eng">${esc((ev && ev.engine) || data.analysis_engine || "Stockfish")}${ev && ev.depth && ev.depth < 99 ? " d" + ev.depth : ""}</span></div>`}
    <div class="comment"><div class="who">${commentWho}</div>${esc(shownMove ? shownMove.comment : "")}</div>
    ${game.termination ? `<div class="comment"><div class="who">Result</div>${esc(game.termination)}</div>` : ""}
    <div class="moves">${moveText || "&nbsp;"}</div>
    <div class="nav"><button data-nav="first">|&lt;</button><button data-nav="prev">&lt;</button><button data-nav="next">&gt;</button><button data-nav="last">&gt;|</button>
      ${pinned ? `<button data-nav="close">Back to live</button>` : ""}<span class="ply">ply ${ply} / ${moves.length}</span></div>
  </div>`;
}

function render() {
  if (!data || !data.id) return;
  document.getElementById("title").textContent = data.title || "AI Chess Swiss";
  document.title = data.title || "AI Chess Swiss";
  const cfg = data.config || {};
  const finished = data.finished;
  document.getElementById("chips").innerHTML = [
    finished ? `<span class="chip done">Finished - winner <b>${esc(data.winner || "")}</b></span>` : `<span class="chip live">Round <b>${data.current_round}</b> of ${cfg.rounds}</span>`,
    data.paused ? `<span class="chip" title="${esc(data.paused)}">Paused: a provider is unavailable; the game will be replayed</span>` : "",
    `<span class="chip"><b>${Math.round((cfg.timeControlMs || 0) / 60000)} min${cfg.incrementMs ? " + " + Math.round(cfg.incrementMs / 1000) + " s" : ""}</b> per player</span>`,
    `<span class="chip"><b>${cfg.maxAttempts}</b> tries per move, then forfeit</span>`,
    `<span class="chip">Swiss, Elo start <b>${cfg.startElo}</b>, K=${cfg.eloK}</span>`,
    data.analysis_engine ? `<span class="chip btn ${analysisOn ? "on" : ""}" data-toggle-analysis title="Viewer-only engine analysis; the AI players never see it">${esc(data.analysis_engine)} analysis: <b>${analysisOn ? "on" : "off"}</b></span>` : "",
  ].join("");
  const games = data.games || {};
  const rnd = (data.rounds || []).find(r => r.round === data.current_round);
  let shown = rnd ? rnd.pairings.map(p => games[p.game_id]).filter(Boolean) : [];
  if (selected && games[selected] && !shown.some(g => g.id === selected)) shown = [games[selected]].concat(shown.filter(g => g.status === "live"));
  const boards = document.getElementById("boards");
  boards.innerHTML = shown.length ? shown.map(gameCard).join("") : `<div class="empty card">Waiting for pairings...</div>`;
  if (rnd && rnd.bye) boards.insertAdjacentHTML("beforeend", "");
  const rows = (data.standings || []).map(r => {
    const d = r.elo_delta || 0;
    return `<tr class="${r.rank === 1 && (r.played || 0) > 0 ? "rank1" : ""}"><td>${r.rank}</td><td class="player">${esc(r.name)}<span class="route">${esc(route(r.name))}</span></td>`
      + `<td class="num"><b>${r.points}</b></td><td class="num">${Math.round(r.elo)} <span class="${d > 0 ? "up" : d < 0 ? "down" : ""}">${Math.round(d) ? (d > 0 ? "+" : "") + Math.round(d) : ""}</span></td>`
      + `<td class="num">${r.wins}/${r.draws}/${r.losses}</td><td class="num">${r.forfeits}</td><td class="num">${r.flags}</td><td class="num">${r.invalid_attempts}</td></tr>`;
  }).join("");
  document.getElementById("standings").innerHTML = `<table><thead><tr><th>#</th><th>Player</th><th class="num">Pts</th><th class="num">Elo</th><th class="num">W/D/L</th><th class="num" title="Lost by 3 invalid replies">Forf</th><th class="num" title="Lost on time">Flag</th><th class="num" title="Rejected replies">Bad</th></tr></thead><tbody>${rows}</tbody></table>`;
  document.getElementById("rounds").innerHTML = (data.rounds || []).slice().reverse().map(r => {
    const pairs = r.pairings.map(p => {
      const g = games[p.game_id] || {};
      const res = g.status === "live" ? `<span class="r live">LIVE</span>` : `<span class="r">${esc(g.result && g.result !== "*" ? g.result.replace("1/2-1/2", "½-½") : "-")}</span>`;
      return `<div class="pair ${selected === p.game_id ? "sel" : ""}" data-pick="${esc(p.game_id)}"><span class="w">${esc(p.white)}</span>${res}<span class="b">${esc(p.black)}</span>`
        + (g.termination ? `<span class="why">${esc(g.termination)}</span>` : "") + `</div>`;
    }).join("");
    return `<div class="round"><div class="round-title"><span>Round ${r.round}</span><span>${r.status === "finished" ? "done" : "playing"}</span></div>${pairs}${r.bye ? `<div class="bye">Bye: ${esc(r.bye)} (sits out this round${cfg.byePoints ? `, +${cfg.byePoints}` : ", no points"})</div>` : ""}</div>`;
  }).join("") || `<div class="empty">No rounds yet</div>`;
  document.getElementById("rules").innerHTML = [
    "Each AI picks every move itself: no tools, no code, no chess engine.",
    `${cfg.maxAttempts} replies per move; an illegal or broken reply is rejected with the reason, the third one forfeits the game.`,
    `${Math.round((cfg.timeControlMs || 0) / 60000)} minutes of model thinking time per player, ${cfg.incrementMs ? "+" + Math.round(cfg.incrementMs / 1000) + " s per move" : "no increment"}; the clock runs out = loss on time.`,
    "Every model thinks at High effort. Past the move cap (1.5x its time budget) its thinking stops, it gets all of that thinking back and gives its move. Running out of time is never an invalid reply.",
    "Points: 1 for a win, 0.5 for a draw, 0 for a loss or a bye.",
    "Swiss pairing: same score meets same score, no rematches, one bye each.",
    "Stockfish analysis is for viewers only: the AI players never see it.",
  ].map(x => `<li>${esc(x)}</li>`).join("");
}

document.addEventListener("click", ev => {
  if (ev.target.closest("[data-toggle-analysis]")) {
    analysisOn = !analysisOn;
    try { localStorage.setItem("swissAnalysis", analysisOn ? "on" : "off"); } catch (e) { /* storage blocked */ }
    render();
    return;
  }
  const pick = ev.target.closest("[data-pick]");
  if (pick) { selected = pick.dataset.pick; replayPly = null; render(); return; }
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
  if (!selected || !data.games[selected]) return;
  const total = (data.games[selected].moves || []).length;
  const cur = replayPly === null ? total : replayPly;
  if (ev.key === "ArrowLeft") { replayPly = Math.max(0, cur - 1); render(); }
  if (ev.key === "ArrowRight") { replayPly = Math.min(total, cur + 1); render(); }
  if (ev.key === "Escape") { selected = null; replayPly = null; render(); }
});

async function poll() {
  try {
    const q = params.get("id") ? `?id=${encodeURIComponent(params.get("id"))}` : "";
    const res = await fetch(`/api/tournament${q}`, { cache: "no-store" });
    if (res.ok) { data = await res.json(); render(); }
  } catch (e) { /* keep the last frame */ }
}
poll();
setInterval(poll, 1000);
setInterval(render, 500);   // clocks tick between polls
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

    def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
            if self.analyzer is not None:
                state = json.loads(body)
                live = {gid: g.get("fen") for gid, g in (state.get("games") or {}).items()
                        if g.get("status") == "live" and g.get("fen")}
                self.analyzer.want(live=list(live.values()))
                state["analysis_engine"] = self.analyzer.name
                state["analysis"] = {gid: self.analyzer.get(fen) for gid, fen in live.items()}
                body = json.dumps(state).encode("utf-8")
            self._send(200, body, "application/json")
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
    parser.add_argument("--no-analysis", action="store_true")
    args = parser.parse_args(argv)
    Handler.state_path = args.state.resolve() if args.state else None
    Handler.live_dir = args.live_dir.resolve()
    if not args.no_analysis:
        if args.engine.exists():
            Handler.analyzer = Analyzer(args.engine)
            print(f"analysis: {Handler.analyzer.name} ({args.engine})", flush=True)
        else:
            print(f"analysis off: engine not found at {args.engine}", flush=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"tournament viewer on http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
