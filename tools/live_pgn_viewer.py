import argparse
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import chess
import chess.pgn


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "out"
DEFAULT_PGN_PATH = OUT_DIR / "live" / "codex-vs-stockfish-live.pgn"
DEFAULT_ENGINE_CONFIG = Path.home() / "AppData/Roaming/org.encroissant.app/engines/engines.json"
LEARNER_DIR = ROOT / "engines" / "codex-chess-learner"
LEARNER_MEMORY_PATH = LEARNER_DIR / "MEMORY.md"
LEARNER_SKILLS_DIR = LEARNER_DIR / "skills"
LEARNER_KNOWLEDGEBASE_DIR = LEARNER_DIR / "knowledgebase"
ENGINE_LOG_DIR = OUT_DIR / "codex-chess-logs"
CLK_COMMENT_RE = re.compile(r"\[%clk\s+(?P<value>\d+(?::\d{1,2}){1,2}(?:\.\d+)?)\]")
LIVE_STATUS_MAX_AGE_SECONDS = 3 * 60
HOT_RELOAD_ENV = "CHESS_VIEWER_HOT_RELOAD_CHILD"
HOT_RELOAD_FILES = (
    Path(__file__).resolve(),
    ROOT / "README.md",
    ROOT / "PRD.md",
    ROOT / "PRD_CHECKLIST.md",
    ROOT / "AGENTS.md",
    ROOT / "chess-harness.config.json",
)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Chess Engine Viewer</title>
  <style>
    /* === DESIGN TOKENS === */
    :root {
      color-scheme: light;
      --bg:           #F5F5F7;
      --surface:      #FFFFFF;
      --surface-alt:  #F2F2F7;
      --line:         #E5E5EA;
      --ctrl-bg:      #FFFFFF;
      --ctrl-border:  #C7C7CC;
      --text:         #1C1C1E;
      --muted:        #6E6E73;
      --accent:       #007AFF;
      --accent-bg:    #EAF3FF;
      --danger:       #FF3B30;
      --warn:         #FF9500;
      --ok:           #34C759;
      --board-light:  #F0D9B5;
      --board-dark:   #B58863;
      --r-lg: 14px; --r-md: 9px; --r-sm: 6px;
      --sh-sm:   0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.05);
      --sh-md:   0 4px 10px rgba(0,0,0,.09), 0 2px 4px rgba(0,0,0,.05);
      --sh-board: 0 3px 14px rgba(0,0,0,.18), 0 0 0 1px rgba(0,0,0,.07);
    }
    [data-theme="dark"] {
      color-scheme: dark;
      --bg:          #000000;
      --surface:     #1C1C1E;
      --surface-alt: #2C2C2E;
      --line:        #38383A;
      --ctrl-bg:     #2C2C2E;
      --ctrl-border: #48484A;
      --text:        #FFFFFF;
      --muted:       #8E8E93;
      --accent:      #0A84FF;
      --accent-bg:   #0A2744;
      --danger:      #FF453A;
      --warn:        #FF9F0A;
      --ok:          #30D158;
      --board-light: #D4B896;
      --board-dark:  #997755;
      --sh-sm:   0 1px 3px rgba(0,0,0,.4);
      --sh-md:   0 4px 10px rgba(0,0,0,.45);
      --sh-board: 0 3px 14px rgba(0,0,0,.55), 0 0 0 1px rgba(0,0,0,.25);
    }

    /* === RESET === */
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
      font-size: 14px;
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
    }

    /* === CONTROLS === */
    button, select, input, textarea { font: inherit; }

    button {
      display: inline-flex; align-items: center; justify-content: center; gap: 5px;
      border: 1px solid var(--ctrl-border);
      background: var(--ctrl-bg);
      color: var(--text);
      border-radius: var(--r-md);
      padding: 6px 13px;
      cursor: pointer;
      font-size: 13px; font-weight: 500;
      transition: background .12s, opacity .12s;
      white-space: nowrap;
    }
    button:hover:not(:disabled) { background: var(--surface-alt); }
    button.primary {
      border-color: var(--accent); background: var(--accent); color: #fff;
    }
    button.primary:hover:not(:disabled) { opacity: .88; }
    button:disabled { opacity: .35; cursor: default; }

    select,
    input:not([type="checkbox"]),
    textarea {
      width: 100%;
      border: 1px solid var(--ctrl-border);
      border-radius: var(--r-md);
      background: var(--ctrl-bg);
      color: var(--text);
      padding: 7px 10px;
      font-size: 13px;
      outline: none;
      transition: border-color .15s, box-shadow .15s;
    }
    select:focus, input:focus, textarea:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--accent-bg);
    }
    input[type="checkbox"] {
      width: 15px; height: 15px;
      accent-color: var(--accent);
      cursor: pointer; flex-shrink: 0;
    }
    textarea {
      min-height: 220px; resize: vertical;
      font-family: "SF Mono", "Menlo", "Cascadia Code", "Consolas", monospace;
      font-size: 12px; line-height: 1.55;
    }
    label {
      display: grid; gap: 4px;
      color: var(--muted);
      font-size: 11px; font-weight: 600;
      text-transform: uppercase; letter-spacing: .04em;
    }

    /* === SHELL === */
    .app { min-height: 100vh; display: grid; grid-template-rows: auto auto 1fr; }

    /* === TOPBAR === */
    .topbar {
      display: flex; align-items: center;
      justify-content: space-between; gap: 16px;
      padding: 10px 20px;
      background: rgba(255,255,255,.85);
      border-bottom: 1px solid var(--line);
      position: sticky; top: 0; z-index: 100;
      backdrop-filter: saturate(180%) blur(20px);
      -webkit-backdrop-filter: saturate(180%) blur(20px);
    }
    [data-theme="dark"] .topbar { background: rgba(28,28,30,.85); }

    .brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
    .brand-icon {
      width: 30px; height: 30px;
      background: #1C1C1E; border-radius: 7px;
      display: grid; place-items: center;
      font-size: 18px; line-height: 1; flex-shrink: 0;
      box-shadow: var(--sh-sm);
    }
    [data-theme="dark"] .brand-icon { background: var(--surface-alt); border: 1px solid var(--line); }
    .brand-name { font-size: 15px; font-weight: 700; letter-spacing: -.01em; white-space: nowrap; }
    .brand-meta {
      display: flex; align-items: center; gap: 6px;
      min-width: 0; max-width: 420px;
    }
    .brand-path { font-size: 11px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
    .copy-path-btn {
      flex-shrink: 0;
      width: 24px; height: 24px; padding: 0;
      border-radius: var(--r-sm);
      font-size: 14px; line-height: 1;
    }
    .copy-path-btn.copied { border-color: var(--ok); color: var(--ok); }
    .current-game-banner {
      color: var(--text);
      font-size: 16px;
      font-weight: 800;
      line-height: 1.25;
      padding: 10px 20px 0;
      overflow-wrap: anywhere;
    }

    .top-right { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }

    .view-tabs {
      display: inline-flex; align-items: center;
      border: 1px solid var(--ctrl-border); border-radius: var(--r-md); overflow: hidden;
    }
    .view-tabs button {
      border: none; border-radius: 0;
      height: 32px; padding: 0 12px;
      font-size: 12px; font-weight: 600;
      background: var(--ctrl-bg);
    }
    .view-tabs button + button { border-left: 1px solid var(--ctrl-border); }
    .view-tabs button.active { background: var(--text); color: var(--surface); }

    .nav-cluster {
      display: inline-flex; align-items: center;
      border: 1px solid var(--ctrl-border); border-radius: var(--r-md); overflow: hidden;
    }
    .nav-cluster button {
      border: none; border-radius: 0;
      width: 34px; height: 32px; padding: 0;
      font-size: 15px; background: var(--ctrl-bg);
    }
    .nav-cluster button + button { border-left: 1px solid var(--ctrl-border); }

    .pill-toggle {
      display: inline-flex; align-items: center; gap: 5px;
      font-size: 12px; font-weight: 500;
      color: var(--muted); cursor: pointer;
      white-space: nowrap; user-select: none;
    }

    .status-pill {
      display: inline-flex; align-items: center; gap: 5px;
      padding: 4px 10px;
      background: var(--surface-alt);
      border: 1px solid var(--line);
      border-radius: 20px;
      font-size: 11px; font-weight: 600;
      color: var(--muted); white-space: nowrap;
    }
    .pulse { width: 7px; height: 7px; border-radius: 50%; background: var(--warn); flex-shrink: 0; }
    .pulse.ok { background: var(--ok); }

    /* === MAIN GRID === */
    .main {
      width: min(1680px, 100%); margin: 0 auto;
      padding: 20px;
      display: grid;
      grid-template-columns: minmax(280px, 360px) minmax(420px, 1fr) minmax(300px, 420px);
      gap: 16px; align-items: start;
    }
    .left-col, .center-col, .side-col { display: grid; gap: 16px; min-width: 0; }
    .thinking-card {
      position: sticky; top: 70px;
      height: 520px;
      display: grid; grid-template-rows: auto auto auto minmax(0, 1fr);
    }
    .thinking-card .card-body { overflow: auto; min-height: 0; }
    .view-panel.hidden { display: none !important; }

    .learner-main {
      width: min(1440px, 100%); margin: 0 auto;
      padding: 20px;
      display: grid;
      grid-template-columns: minmax(320px, 420px) minmax(360px, 1fr);
      gap: 20px; align-items: start;
    }
    .learner-col { display: grid; gap: 16px; min-width: 0; }
    .learner-summary { display: grid; gap: 0; }
    .summary-row {
      display: grid; grid-template-columns: 138px 1fr;
      gap: 12px; padding: 8px 0;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
    }
    .summary-row:last-child { border-bottom: none; }
    .summary-row span:first-child { color: var(--muted); }
    .doc-text {
      max-height: 360px; overflow: auto;
      margin: 0; padding: 12px;
      border: 1px solid var(--line); border-radius: var(--r-sm);
      background: var(--surface-alt);
      font-family: "SF Mono", "Menlo", "Cascadia Code", "Consolas", monospace;
      font-size: 12px; line-height: 1.55; white-space: pre-wrap;
    }
    .file-list, .log-list { display: grid; gap: 8px; }
    .segmented {
      display: inline-flex; align-items: center; gap: 2px;
      padding: 2px; border: 1px solid var(--line); border-radius: 999px;
      background: var(--surface-alt);
    }
    .segmented button {
      min-height: 24px; padding: 2px 9px;
      border: 0; border-radius: 999px; background: transparent;
      color: var(--muted); font-size: 11px; font-weight: 700;
      text-transform: uppercase; letter-spacing: .04em; cursor: pointer;
    }
    .segmented button.active {
      color: var(--text); background: var(--surface);
      box-shadow: 0 1px 3px rgba(15, 23, 42, .12);
    }
    .log-filter-row {
      display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
      padding: 8px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--surface);
    }
    .log-filter-label {
      color: var(--muted); font-size: 11px; font-weight: 700;
      text-transform: uppercase; letter-spacing: .04em;
    }
    .multi-filter {
      display: flex; align-items: center; gap: 4px; flex-wrap: wrap;
    }
    .multi-filter button {
      min-height: 24px; padding: 2px 8px;
      border: 1px solid var(--line); border-radius: 7px;
      background: var(--surface-alt);
      color: var(--muted); font-size: 11px; font-weight: 700;
      text-transform: uppercase; letter-spacing: .04em; cursor: pointer;
    }
    .multi-filter button.active {
      color: var(--text);
      background: var(--surface);
      border-color: var(--text);
    }
    .file-row {
      border: 1px solid var(--line); border-radius: var(--r-md);
      background: var(--surface); overflow: hidden;
    }
    .file-row summary {
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 9px 11px; cursor: pointer; font-size: 13px; font-weight: 600;
    }
    .file-meta { color: var(--muted); font-size: 11px; font-weight: 500; white-space: nowrap; }
    .file-row .doc-text { border: none; border-top: 1px solid var(--line); border-radius: 0; max-height: 260px; }
    .log-entry {
      border: 1px solid var(--line); border-left-width: 4px;
      border-radius: var(--r-md); padding: 8px 10px;
      background: var(--surface);
    }
    .log-entry.learner { border-left-color: #34C759; }
    .log-entry.baseline { border-left-color: #0A84FF; }
    .log-entry.unknown { border-left-color: var(--warn); }
    .log-entry.white { box-shadow: inset 0 0 0 1px rgba(10, 132, 255, .12); }
    .log-entry.black { box-shadow: inset 0 0 0 1px rgba(255, 149, 0, .14); }
    .log-head {
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
      margin-bottom: 4px; font-size: 11px; color: var(--muted);
    }
    .log-kind {
      display: inline-flex; align-items: center;
      border: 1px solid var(--line); border-radius: 999px;
      padding: 1px 6px; margin-left: 6px;
      font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em;
    }
    .log-side {
      display: inline-flex; align-items: center;
      border: 1px solid var(--line); border-radius: 999px;
      padding: 1px 6px; margin-left: 5px;
      font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em;
    }
    .log-side.white { color: #0A84FF; }
    .log-side.black { color: #A05A00; }
    .bot-name { font-weight: 700; color: var(--text); }
    .log-text {
      font-family: "SF Mono", "Menlo", "Cascadia Code", "Consolas", monospace;
      font-size: 12px; line-height: 1.45; overflow-wrap: anywhere;
    }

    /* === CARD === */
    .card {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--r-lg);
      box-shadow: var(--sh-sm);
      overflow: hidden;
    }
    .card-hd {
      display: flex; align-items: center;
      justify-content: space-between; gap: 10px;
      padding: 11px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--surface-alt);
    }
    .card-title { font-size: 13px; font-weight: 600; }
    .card-sub { font-size: 11px; color: var(--muted); }
    .card-body { padding: 14px 16px; }

    /* === BOARD CARD === */
    .board-card {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--r-lg);
      box-shadow: var(--sh-sm);
      overflow: hidden;
    }
    .board-hd {
      padding: 12px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--surface-alt);
    }
    .board-hd-row {
      display: flex; align-items: center;
      justify-content: space-between; gap: 8px;
      margin-bottom: 6px;
    }
    .game-title { font-size: 14px; font-weight: 700; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
    .game-result { font-size: 14px; font-weight: 700; color: var(--muted); text-align: right; overflow-wrap: anywhere; }
    .player-chips { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 5px; }
    .chip {
      display: inline-flex; align-items: center; gap: 5px;
      border: 1px solid var(--line); border-radius: 20px;
      padding: 3px 9px; font-size: 12px; font-weight: 500;
    }
    .dot-w { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: #fff; border: 1.5px solid #aaa; }
    .dot-b { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: #1C1C1E; border: 1.5px solid #888; }
    .board-turn { font-size: 12px; color: var(--muted); }

    .board-shell { padding: 14px; display: grid; gap: 8px; }
    .player-bar {
      width: min(100%, 66vh); margin: 0 auto;
      display: flex; align-items: center; gap: 8px;
      padding: 7px 12px;
      background: var(--surface-alt); border: 1px solid var(--line); border-radius: var(--r-md);
      font-size: 13px; font-weight: 600;
    }
    .bar-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
    .clock {
      flex-shrink: 0;
      min-width: 62px;
      padding: 3px 7px;
      border: 1px solid var(--line);
      border-radius: var(--r-sm);
      background: var(--surface);
      text-align: center;
      font-variant-numeric: tabular-nums;
      font-feature-settings: "tnum";
      line-height: 1.15;
    }
    .clock.running { border-color: var(--accent); color: var(--accent); }
    .clock.low { border-color: var(--warn); color: var(--warn); }
    .clock.danger { border-color: var(--danger); color: var(--danger); }

    /* === CHESSBOARD === */
    .board {
      width: min(100%, 66vh); aspect-ratio: 1;
      margin: 0 auto;
      border-radius: 3px; overflow: hidden;
      display: grid;
      grid-template-columns: repeat(8, 1fr);
      grid-template-rows: repeat(8, 1fr);
      box-shadow: var(--sh-board);
    }
    .sq {
      position: relative;
      display: grid; place-items: center;
      min-width: 0; min-height: 0;
    }
    .sq.light { background: var(--board-light); }
    .sq.dark  { background: var(--board-dark); }
    .sq.last::before {
      content: ""; position: absolute; inset: 0;
      background: rgba(20,85,30,.38);
      pointer-events: none; z-index: 0;
    }
    .coord {
      position: absolute;
      font-size: 9.5px; font-weight: 700; line-height: 1;
      pointer-events: none; z-index: 2;
      font-family: -apple-system, sans-serif;
    }
    .coord.file { bottom: 2px; right: 3px; }
    .coord.rank { top: 2px; left: 3px; }
    .sq.light .coord { color: var(--board-dark); opacity: .75; }
    .sq.dark  .coord { color: var(--board-light); opacity: .75; }
    .piece {
      width: 88%; height: 88%;
      display: flex; align-items: center; justify-content: center;
      position: relative; z-index: 1; pointer-events: none;
    }
    .piece svg { width: 100%; height: 100%; display: block; }

    /* === MOVES TABLE === */
    .moves-tbl { width: 100%; border-collapse: collapse; }
    .moves-tbl th, .moves-tbl td {
      padding: 5px 10px; border-bottom: 1px solid var(--line);
      text-align: left; vertical-align: middle; font-size: 13px;
    }
    .moves-tbl th {
      font-size: 11px; font-weight: 600; color: var(--muted);
      text-transform: uppercase; letter-spacing: .04em;
      background: var(--surface-alt); padding: 7px 10px;
    }
    .moves-tbl td:first-child { width: 42px; color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
    .moves-tbl tbody tr:last-child td { border-bottom: none; }
    .moves-tbl tbody tr:hover td { background: var(--surface-alt); }

    /* === STATS TABLE === */
    .stats-tbl { width: 100%; border-collapse: collapse; }
    .stats-tbl th, .stats-tbl td {
      padding: 5px 10px; border-bottom: 1px solid var(--line);
      text-align: left; font-size: 13px;
    }
    .stats-tbl th {
      font-size: 11px; font-weight: 600; color: var(--muted);
      text-transform: uppercase; letter-spacing: .04em;
      background: var(--surface-alt); padding: 7px 10px;
    }
    .stats-tbl td.n { text-align: right; font-variant-numeric: tabular-nums; }
    .stats-tbl td.rk { width: 34px; color: var(--muted); font-variant-numeric: tabular-nums; }
    .stats-tbl tbody tr:last-child td { border-bottom: none; }
    .stats-tbl tbody tr:hover td { background: var(--surface-alt); }
    .pager {
      display: inline-flex; align-items: center; gap: 6px;
    }
    .pager button {
      min-height: 24px; padding: 2px 8px;
      border: 1px solid var(--line); border-radius: var(--r-sm);
      background: var(--surface); color: var(--text); cursor: pointer;
    }
    .pager button:disabled { opacity: .45; cursor: not-allowed; }
    .side-controls {
      display: flex; justify-content: flex-end;
    }
    .match-list { display: grid; gap: 8px; }
    .match-row {
      border: 1px solid var(--line); border-radius: var(--r-md);
      padding: 9px 10px; background: var(--surface);
      display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px;
      align-items: start;
      width: 100%; color: var(--text); text-align: left;
    }
    .match-row:hover, .match-row.active { border-color: var(--accent); background: rgba(10, 132, 255, .08); }
    .match-row.in-progress { border-color: rgba(48, 209, 88, .45); }
    .match-row.disabled { opacity: .78; }
    .match-select {
      min-width: 0; width: 100%; max-width: 100%; padding: 0; border: 0; background: transparent;
      color: inherit; text-align: left; cursor: pointer; display: grid; gap: 4px;
      font: inherit;
    }
    .match-select:disabled { cursor: default; }
    .match-main {
      min-width: 0;
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
      font-size: 13px; font-weight: 700;
    }
    .match-main span { min-width: 0; overflow-wrap: anywhere; }
    .match-winner { color: var(--text); font-weight: 700; }
    .match-sub {
      display: block; min-width: 0; max-width: 100%;
      font-size: 11px; line-height: 1.35; color: var(--muted);
      white-space: normal; overflow: visible;
      overflow-wrap: anywhere; word-break: break-word;
    }
    .match-copy-btn {
      display: none; align-self: center; place-items: center;
      width: 24px; height: 24px; padding: 0; flex-shrink: 0;
      border-radius: var(--r-sm); font-size: 14px; line-height: 1;
    }
    .match-row.active .match-copy-btn { display: grid; }
    .match-copy-btn.copied { border-color: var(--ok); color: var(--ok); }
    .status-badge {
      display: inline-flex; align-items: center; min-height: 20px; padding: 2px 7px;
      border-radius: 999px; border: 1px solid var(--line);
      color: var(--muted); font-size: 11px; font-weight: 700; white-space: nowrap;
    }
    .status-badge.in-progress { border-color: rgba(48, 209, 88, .55); color: var(--ok); }

    /* === ANALYSIS === */
    .analysis-list { display: grid; gap: 0; }
    .analysis-line {
      display: grid; grid-template-columns: 64px 1fr;
      gap: 12px; align-items: start;
      padding: 10px 0; border-bottom: 1px solid var(--line);
    }
    .analysis-line:last-child { border-bottom: none; }
    .score { font-size: 14px; font-weight: 700; font-variant-numeric: tabular-nums; }
    .pv { font-size: 13px; min-width: 0; overflow-wrap: anywhere; }
    .pv-meta { font-size: 11px; color: var(--muted); margin-top: 2px; }
    .analysis-card { position: sticky; top: 606px; }

    /* === ENGINE CONFIG === */
    .config-card > summary { list-style: none; cursor: pointer; }
    .config-card > summary::-webkit-details-marker { display: none; }
    .config-card .config-caret { color: var(--muted); font-size: 12px; }
    .config-card[open] .config-caret::before { content: "Collapse"; }
    .config-card:not([open]) .config-caret::before { content: "Expand"; }
    .cfg-bar { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
    .cfg-mode { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; font-weight: 500; color: var(--muted); cursor: pointer; text-transform: none; letter-spacing: 0; }
    .engine-list { display: grid; gap: 10px; }
    .eng-card { border: 1px solid var(--line); border-radius: var(--r-md); overflow: hidden; }
    .eng-head {
      display: grid; grid-template-columns: 1fr 80px 72px;
      gap: 10px; align-items: end; padding: 10px 12px;
      background: var(--surface-alt); border-bottom: 1px solid var(--line);
    }
    .eng-fields, .eng-settings { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; padding: 10px 12px; }
    .eng-settings-title { grid-column: 1/-1; font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
    .full-col { grid-column: 1/-1; }
    .cfg-actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 12px; }

    /* === MISC === */
    .empty { padding: 14px 0; color: var(--muted); font-size: 13px; }
    .err { color: var(--danger); }
    .ok-txt { color: var(--ok); }
    .hidden { display: none !important; }

    /* === RESPONSIVE === */
    @media (max-width: 860px) {
      .topbar { flex-direction: column; align-items: flex-start; }
      .brand { width: 100%; }
      .top-right { width: 100%; justify-content: space-between; }
      .brand-meta { max-width: 100%; }
      .brand-path { max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
      .main, .learner-main { grid-template-columns: 1fr; padding: 12px; gap: 14px; }
      .board-shell { padding: 10px; }
      .eng-head, .eng-fields, .eng-settings { grid-template-columns: 1fr; }
      .summary-row { grid-template-columns: 1fr; gap: 2px; }
      .summary-row strong, .file-row summary, .log-text { min-width: 0; overflow-wrap: anywhere; }
      .thinking-card { position: static; max-height: none; }
      .thinking-card .card-body { max-height: 420px; }
    }
    @media (min-width: 861px) and (max-width: 1180px) {
      .main { grid-template-columns: minmax(280px, 360px) minmax(420px, 1fr); }
      .side-col { grid-column: 1 / -1; grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .side-col .card:nth-child(n + 3) { grid-column: span 1; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <div class="brand-icon">&#9823;</div>
        <div>
          <div class="brand-name">Chess Engine Viewer</div>
          <div class="brand-meta">
            <div id="pgn-path" class="brand-path"></div>
            <button id="copy-pgn-path" class="copy-path-btn" type="button" title="Copy match URL" aria-label="Copy match URL" disabled>&#10697;</button>
          </div>
        </div>
      </div>
      <div class="top-right">
        <div class="view-tabs" role="tablist" aria-label="Viewer screen">
          <button id="board-view-tab" class="active" type="button">Board</button>
          <button id="learner-view-tab" type="button">Learner</button>
        </div>
        <label class="pill-toggle"><input id="follow-toggle" type="checkbox" checked> Follow live</label>
        <div class="nav-cluster">
          <button id="prev-move" type="button" title="Previous move" aria-label="Previous move">&#8592;</button>
          <button id="next-move" type="button" title="Next move" aria-label="Next move">&#8594;</button>
        </div>
        <label class="pill-toggle"><input id="analysis-toggle" type="checkbox" checked> Analysis</label>
        <button id="theme-toggle" type="button">Dark</button>
        <div class="status-pill">
          <span id="status-dot" class="pulse"></span>
          <span id="status-text">Waiting</span>
        </div>
      </div>
    </header>

    <div id="current-game-title" class="current-game-banner">No game loaded</div>

    <main id="board-view" class="main view-panel">
      <aside class="left-col">
        <section class="card thinking-card">
          <div class="card-hd">
            <span class="card-title">Bot Thinking</span>
            <div class="segmented log-side-filter" role="group" aria-label="Thinking side filter">
              <button class="active" type="button" data-log-side="all">All</button>
              <button type="button" data-log-side="white">White</button>
              <button type="button" data-log-side="black">Black</button>
            </div>
          </div>
          <div class="log-filter-row" aria-label="Thinking message type filter">
            <span class="log-filter-label">Type</span>
            <div class="multi-filter log-kind-filter">
              <button type="button" data-log-kind="all">All</button>
              <button type="button" data-log-kind="setup">Setup</button>
              <button type="button" data-log-kind="prompt">Prompt</button>
              <button type="button" data-log-kind="comment">Comment</button>
              <button type="button" data-log-kind="move">Move</button>
              <button type="button" data-log-kind="repair">Repair</button>
              <button type="button" data-log-kind="account">Account</button>
              <button type="button" data-log-kind="log">Log</button>
            </div>
          </div>
          <div id="board-thinking-meta" class="card-sub" style="padding:0 16px 8px"></div>
          <div id="board-thinking-logs" class="card-body"></div>
        </section>

        <section class="card analysis-card">
          <div class="card-hd">
            <span id="analysis-title" class="card-title">Engine Analysis</span>
            <label class="pill-toggle" style="font-size:12px;text-transform:none;letter-spacing:0"><input id="analysis-panel-toggle" type="checkbox" checked> On</label>
          </div>
          <div id="analysis-meta" class="card-sub" style="padding:5px 16px 0"></div>
          <div id="analysis" class="card-body" style="padding-top:6px"></div>
        </section>
      </aside>

      <div class="center-col">
        <section class="board-card">
          <div class="board-hd">
            <div class="board-hd-row">
              <span id="players" class="game-title">No game loaded</span>
              <span id="result" class="game-result">&#42;</span>
            </div>
            <div class="player-chips">
              <span id="tournament-chip" class="chip">Tournament: &#8212;</span>
              <span id="white-player" class="chip"><span class="dot-w"></span>White: &#8212;</span>
              <span id="black-player" class="chip"><span class="dot-b"></span>Black: &#8212;</span>
            </div>
            <div id="turn" class="board-turn">&#8212;</div>
          </div>
          <div class="board-shell">
            <div id="top-player" class="player-bar">
              <span class="dot-b"></span>
              <span class="bar-name">Black: &#8212;</span>
              <span id="black-clock" class="clock">--:--</span>
            </div>
            <div id="board" class="board" aria-label="Chess board"></div>
            <div id="bottom-player" class="player-bar">
              <span class="dot-w"></span>
              <span class="bar-name">White: &#8212;</span>
              <span id="white-clock" class="clock">--:--</span>
            </div>
          </div>
        </section>
      </div>

      <div class="side-col">
        <div class="side-controls">
          <button id="flip-board" type="button" title="Flip board" aria-label="Flip board">Flip Board</button>
        </div>
        <section class="card">
          <div class="card-hd">
            <span class="card-title">Leaderboard</span>
            <span id="stats-meta" class="card-sub"></span>
          </div>
          <div id="stats" class="card-body"></div>
        </section>

        <section class="card">
          <div class="card-hd">
            <span class="card-title">Matches</span>
            <div class="pager">
              <button id="matches-prev" type="button" aria-label="Previous matches page">&#8592;</button>
              <span id="matches-meta" class="card-sub"></span>
              <button id="matches-next" type="button" aria-label="Next matches page">&#8594;</button>
            </div>
          </div>
          <div id="matches" class="card-body"></div>
        </section>

        <section class="card">
          <div class="card-hd">
            <span class="card-title">Move List</span>
            <span id="meta" class="card-sub"></span>
          </div>
          <div id="moves"></div>
        </section>

        <details id="engine-config-card" class="card config-card">
          <summary class="card-hd">
            <span class="card-title">Engine Config</span>
            <span class="card-sub"><span id="config-path"></span><span class="config-caret"></span></span>
          </summary>
          <div class="card-body">
            <div class="cfg-bar">
              <label class="cfg-mode"><input id="raw-config-toggle" type="checkbox"> Raw JSON</label>
              <span id="config-status" class="card-sub"></span>
            </div>
            <div id="config-controls" class="engine-list"></div>
            <div id="raw-config-wrap" class="hidden">
              <textarea id="config-json" spellcheck="false" aria-label="Engine config JSON"></textarea>
            </div>
            <div class="cfg-actions">
              <button id="config-save" class="primary" type="button">Save Config</button>
            </div>
          </div>
        </details>
      </div>
    </main>

    <main id="learner-view" class="learner-main view-panel hidden">
      <div class="learner-col">
        <section class="card">
          <div class="card-hd">
            <span class="card-title">Learner Data</span>
            <span id="learner-updated" class="card-sub"></span>
          </div>
          <div id="learner-summary" class="card-body"></div>
        </section>

        <section class="card">
          <div class="card-hd">
            <span class="card-title">Memory</span>
            <span id="learner-memory-meta" class="card-sub"></span>
          </div>
          <div class="card-body">
            <pre id="learner-memory-text" class="doc-text"></pre>
          </div>
        </section>
      </div>

      <div class="learner-col">
        <section class="card">
          <div class="card-hd">
            <span class="card-title">Knowledgebase</span>
            <span id="learner-kb-meta" class="card-sub"></span>
          </div>
          <div id="learner-kb" class="card-body"></div>
        </section>

        <section class="card">
          <div class="card-hd">
            <span class="card-title">Skills</span>
            <span id="learner-skills-meta" class="card-sub"></span>
          </div>
          <div id="learner-skills" class="card-body"></div>
        </section>

        <section class="card">
          <div class="card-hd">
            <span class="card-title">Bot Logs</span>
            <div class="segmented log-side-filter" role="group" aria-label="Learner log side filter">
              <button class="active" type="button" data-log-side="all">All</button>
              <button type="button" data-log-side="white">White</button>
              <button type="button" data-log-side="black">Black</button>
            </div>
          </div>
          <div class="log-filter-row" aria-label="Learner log message type filter">
            <span class="log-filter-label">Type</span>
            <div class="multi-filter log-kind-filter">
              <button type="button" data-log-kind="all">All</button>
              <button type="button" data-log-kind="setup">Setup</button>
              <button type="button" data-log-kind="prompt">Prompt</button>
              <button type="button" data-log-kind="comment">Comment</button>
              <button type="button" data-log-kind="move">Move</button>
              <button type="button" data-log-kind="repair">Repair</button>
              <button type="button" data-log-kind="account">Account</button>
              <button type="button" data-log-kind="log">Log</button>
            </div>
          </div>
          <div id="learner-logs-meta" class="card-sub" style="padding:0 16px 8px"></div>
          <div id="learner-logs" class="card-body"></div>
        </section>
      </div>
    </main>
  </div>

  <script>
    /* ── SVG piece library (cburnett / standard Staunton) ─────────────────── */
    const PIECES = {
      wK: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 45 45"><g fill="none" fill-rule="evenodd" stroke="#000" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22.5 11.63V6M20 8h5" stroke-linejoin="miter"/><path d="M22.5 25s4.5-7.5 3-10.5c0 0-1-2.5-3-2.5s-3 2.5-3 2.5c-1.5 3 3 10.5 3 10.5" fill="#fff" stroke-linecap="butt" stroke-linejoin="miter"/><path d="M11.5 37c5.5 3.5 15.5 3.5 21 0v-7s9-4.5 6-10.5c-4-6.5-13.5-3.5-16 4V17s-5.5-3.5-13 2c-2 1.5-2 7.5-2 7.5 1.5-2 2.5-2.5 5-1" fill="#fff"/><path d="M11.5 30c5.5-3 15.5-3 21 0m-21 3.5c5.5-3 15.5-3 21 0m-21 3.5c5.5-3 15.5-3 21 0"/></g></svg>`,
      wQ: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 45 45"><g fill="#fff" fill-rule="evenodd" stroke="#000" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="12" r="2.75"/><circle cx="14" cy="9" r="2.75"/><circle cx="22.5" cy="8" r="2.75"/><circle cx="31" cy="9" r="2.75"/><circle cx="39" cy="12" r="2.75"/><path d="M9 26c8.5-8.5 15-7 22.5 0l2.5-12.5L31 25l-.3-14.1-5.2 13.6-3-14.5-3 14.5-5.2-13.6L14 25 6.5 13.5z" stroke-linecap="butt"/><path d="M9 26c0 2 1.5 2 2.5 4 1 1.5 1 1 .5 3.5-1.5 1-1.5 2.5-1.5 2.5-1.5 1.5.5 2.5.5 2.5 6.5 1 16.5 1 23 0 0 0 1.5-1 0-2.5 0 0 .5-1.5-1-2.5-.5-2.5-.5-2 .5-3.5 1-2 2.5-2 2.5-4-8.5-1.5-18.5-1.5-27 0z"/><path d="M11.5 30c3.5-1 18.5-1 26 0" fill="none" stroke-linejoin="miter"/><path d="M12 33.5c4-1.5 17-1.5 25 0" fill="none" stroke-linejoin="miter"/></g></svg>`,
      wR: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 45 45"><g fill="#fff" fill-rule="evenodd" stroke="#000" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 39h27v-3H9v3zm3-3v-4h21v4H12zm-1-22V9h4v2h5V9h5v2h5V9h4v5" stroke-linecap="butt"/><path d="M34 14l-3 3H14l-3-3"/><path d="M31 17v12.5H14V17" stroke-linecap="butt" stroke-linejoin="miter"/><path d="M31 29.5l1.5 2.5h-20l1.5-2.5"/><path d="M11 14h23" fill="none" stroke-linejoin="miter"/></g></svg>`,
      wB: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 45 45"><g fill="none" fill-rule="evenodd" stroke="#000" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><g fill="#fff" stroke-linecap="butt"><path d="M9 36c3.39-.97 10.11.43 13.5-2 3.39 2.43 10.11 1.03 13.5 2 0 0 1.65.54 3 2-.68.97-1.65.99-3 .5-3.39-.97-10.11.46-13.5-1-3.39 1.46-10.11.03-13.5 1-1.354.49-2.323.47-3-.5 1.354-1.94 3-2 3-2z"/><path d="M15 32c2.5 2.5 12.5 2.5 15 0 .5-1.5 0-2 0-2 0-2.5-2.5-4-2.5-4 5.5-1.5 6-11.5-5-15.5-11 4-10.5 14-5 15.5 0 0-2.5 1.5-2.5 4 0 0-.5.5 0 2z"/><path d="M25 8a2.5 2.5 0 1 1-5 0 2.5 2.5 0 1 1 5 0z"/></g><path d="M17.5 26h10M15 30h15m-7.5-14.5v5M20 18h5" stroke-linejoin="miter"/></g></svg>`,
      wN: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 45 45"><g fill="none" fill-rule="evenodd" stroke="#000" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 10c10.5 1 16.5 8 16 29H15c0-9 10-6.5 8-21" fill="#fff"/><path d="M24 18c.38 5.12-5.58 11-10 11.5-4.44 1.08-6 .44-6 .44" fill="#fff"/><path d="M9.5 11.5A5.5 5.5 0 1 0 20.5 11.5 5.5 5.5 0 1 0 9.5 11.5z" fill="#fff"/><path d="M15 15.5c-.39-2.08 5-4.5 5-1.5" fill="#fff" stroke-linecap="butt"/><circle cx="20" cy="12" r="1.5" fill="#000" stroke="none"/><path d="M14 16.5c-1 2 3 4.5 5 2.5" fill="#fff"/></g></svg>`,
      wP: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 45 45"><path d="M22.5 9c-2.21 0-4 1.79-4 4 0 .89.29 1.71.78 2.38C17.33 16.5 16 18.59 16 21c0 2.03.94 3.84 2.41 5.03L15 39.5h15l-3.41-13.47C27.06 24.84 28 23.03 28 21c0-2.41-1.33-4.5-3.28-5.62.49-.67.78-1.49.78-2.38 0-2.21-1.79-4-4-4z" fill="#fff" stroke="#000" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
      bK: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 45 45"><g fill="none" fill-rule="evenodd" stroke="#000" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22.5 11.63V6M20 8h5" stroke-linejoin="miter"/><path d="M22.5 25s4.5-7.5 3-10.5c0 0-1-2.5-3-2.5s-3 2.5-3 2.5c-1.5 3 3 10.5 3 10.5" fill="#000" stroke-linecap="butt" stroke-linejoin="miter"/><path d="M11.5 37c5.5 3.5 15.5 3.5 21 0v-7s9-4.5 6-10.5c-4-6.5-13.5-3.5-16 4V17s-5.5-3.5-13 2c-2 1.5-2 7.5-2 7.5 1.5-2 2.5-2.5 5-1" fill="#000"/><path d="M11.5 30c5.5-3 15.5-3 21 0m-21 3.5c5.5-3 15.5-3 21 0m-21 3.5c5.5-3 15.5-3 21 0" stroke="#fff"/></g></svg>`,
      bQ: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 45 45"><g fill-rule="evenodd" stroke="#000" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="12" r="2.75" fill="#000"/><circle cx="14" cy="9" r="2.75" fill="#000"/><circle cx="22.5" cy="8" r="2.75" fill="#000"/><circle cx="31" cy="9" r="2.75" fill="#000"/><circle cx="39" cy="12" r="2.75" fill="#000"/><path d="M9 26c8.5-8.5 15-7 22.5 0l2.5-12.5L31 25l-.3-14.1-5.2 13.6-3-14.5-3 14.5-5.2-13.6L14 25 6.5 13.5z" fill="#000" stroke-linecap="butt"/><path d="M9 26c0 2 1.5 2 2.5 4 1 1.5 1 1 .5 3.5-1.5 1-1.5 2.5-1.5 2.5-1.5 1.5.5 2.5.5 2.5 6.5 1 16.5 1 23 0 0 0 1.5-1 0-2.5 0 0 .5-1.5-1-2.5-.5-2.5-.5-2 .5-3.5 1-2 2.5-2 2.5-4-8.5-1.5-18.5-1.5-27 0z" fill="#000"/><path d="M11.5 30c3.5-1 18.5-1 26 0" fill="none" stroke="#fff" stroke-linejoin="miter"/><path d="M12 33.5c4-1.5 17-1.5 25 0" fill="none" stroke="#fff" stroke-linejoin="miter"/></g></svg>`,
      bR: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 45 45"><g fill="none" fill-rule="evenodd" stroke="#000" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 39h27v-3H9v3zm3.5-7l1.5-2.5h12l1.5 2.5h-15zm-.5-4V18h16v10H12z" fill="#000" stroke-linecap="butt"/><path d="M14 9h4v3h5V9h5v3h5V9h4v5H11V9h3z" fill="#000" stroke-linecap="butt"/><path d="M34 14l-3 3H14l-3-3"/><path d="M11 14h23" fill="none" stroke-linejoin="miter"/></g></svg>`,
      bB: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 45 45"><g fill="none" fill-rule="evenodd" stroke="#000" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><g fill="#000" stroke-linecap="butt"><path d="M9 36c3.39-.97 10.11.43 13.5-2 3.39 2.43 10.11 1.03 13.5 2 0 0 1.65.54 3 2-.68.97-1.65.99-3 .5-3.39-.97-10.11.46-13.5-1-3.39 1.46-10.11.03-13.5 1-1.354.49-2.323.47-3-.5 1.354-1.94 3-2 3-2z"/><path d="M15 32c2.5 2.5 12.5 2.5 15 0 .5-1.5 0-2 0-2 0-2.5-2.5-4-2.5-4 5.5-1.5 6-11.5-5-15.5-11 4-10.5 14-5 15.5 0 0-2.5 1.5-2.5 4 0 0-.5.5 0 2z"/><path d="M25 8a2.5 2.5 0 1 1-5 0 2.5 2.5 0 1 1 5 0z"/></g><path d="M17.5 26h10M15 30h15m-7.5-14.5v5M20 18h5" stroke="#fff" stroke-linejoin="miter"/></g></svg>`,
      bN: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 45 45"><g fill="none" fill-rule="evenodd" stroke="#000" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 10c10.5 1 16.5 8 16 29H15c0-9 10-6.5 8-21" fill="#000"/><path d="M24 18c.38 5.12-5.58 11-10 11.5-4.44 1.08-6 .44-6 .44" fill="#000"/><path d="M9.5 11.5A5.5 5.5 0 1 0 20.5 11.5 5.5 5.5 0 1 0 9.5 11.5z" fill="#000"/><path d="M15 15.5c-.39-2.08 5-4.5 5-1.5" fill="#000" stroke-linecap="butt"/><circle cx="20" cy="12" r="1.5" fill="#fff" stroke="none"/><path d="M14 16.5c-1 2 3 4.5 5 2.5" fill="#000"/></g></svg>`,
      bP: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 45 45"><path d="M22.5 9c-2.21 0-4 1.79-4 4 0 .89.29 1.71.78 2.38C17.33 16.5 16 18.59 16 21c0 2.03.94 3.84 2.41 5.03L15 39.5h15l-3.41-13.47C27.06 24.84 28 23.03 28 21c0-2.41-1.33-4.5-3.28-5.62.49-.67.78-1.49.78-2.38 0-2.21-1.79-4-4-4z" fill="#000" stroke="#000" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
    };

    const PIECE_KEY = {
      "K":"wK","Q":"wQ","R":"wR","B":"wB","N":"wN","P":"wP",
      "k":"bK","q":"bQ","r":"bR","b":"bB","n":"bN","p":"bP"
    };

    let lastFen = "";
    let configData = [];
    let rawConfigMode = false;
    let engineConfigExpanded = localStorage.getItem("livePgnEngineConfigExpanded") === "on";
    let analysisEnabled = true;
    let analysisAvailable = true;
    let activeTheme = "light";
    let followLive = true;
    let latestGame = null;
    let viewedPly = null;
    let latestClock = null;
    let serverClockOffsetMs = 0;
    let activeView = "board";
    let latestLearnerData = null;
    let logSideFilter = localStorage.getItem("livePgnLogSide") || "all";
    const logKindOptions = ["setup", "prompt", "comment", "move", "repair", "account", "log"];
    const logKindStorageKey = "livePgnLogKindsV2";
    let logKindFilter = new Set(loadLogKindFilter());
    let boardOrientation = localStorage.getItem("livePgnBoardOrientation") === "black" ? "black" : "white";
    let replayThinkingKey = "";
    let selectedMatch = null;
    let requestedLiveGame = null;
    let previousMatches = [];
    let previousMatchesPage = 0;
    let activePgnPath = "";
    let activeLivePgnPath = "";
    let activeMatchUrl = "";
    let suppressHashChange = false;
    const previousMatchesPageSize = 5;

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function escapeAttr(value) {
      return escapeHtml(value).replaceAll("`", "&#96;");
    }

    function loadLogKindFilter() {
      const stored = localStorage.getItem(logKindStorageKey);
      if (stored === null) return ["comment"];
      if (stored === "all") return logKindOptions;
      if (stored === "") return [];
      const values = stored.split(",").map(item => item.trim()).filter(item => logKindOptions.includes(item));
      return values.length ? Array.from(new Set(values)) : ["comment"];
    }

    function applyTheme(theme) {
      activeTheme = theme === "dark" ? "dark" : "light";
      document.body.dataset.theme = activeTheme;
      document.getElementById("theme-toggle").textContent = activeTheme === "dark" ? "Light" : "Dark";
      localStorage.setItem("livePgnTheme", activeTheme);
    }

    function loadPreferences() {
      applyTheme(localStorage.getItem("livePgnTheme") || "light");
      analysisEnabled = localStorage.getItem("livePgnAnalysis") !== "off";
      updateAnalysisControls();
      followLive = true;
      document.getElementById("follow-toggle").checked = followLive;
      syncEngineConfigExpanded();
      updateLogSideButtons();
      updateLogKindButtons();
      updateBoardOrientationButton();
    }

    function syncEngineConfigExpanded() {
      document.getElementById("engine-config-card").open = engineConfigExpanded;
    }

    function setEngineConfigExpanded(enabled) {
      engineConfigExpanded = !!enabled;
      localStorage.setItem("livePgnEngineConfigExpanded", engineConfigExpanded ? "on" : "off");
      syncEngineConfigExpanded();
    }

    function updateLogSideButtons() {
      document.querySelectorAll("[data-log-side]").forEach(button => {
        button.classList.toggle("active", button.dataset.logSide === logSideFilter);
      });
    }

    function setLogSideFilter(side) {
      logSideFilter = ["white", "black"].includes(side) ? side : "all";
      localStorage.setItem("livePgnLogSide", logSideFilter);
      updateLogSideButtons();
      replayThinkingKey = "";
      if (latestLearnerData) renderLearner(latestLearnerData);
    }

    function allLogKindsSelected() {
      return logKindOptions.every(kind => logKindFilter.has(kind));
    }

    function updateLogKindButtons() {
      const allSelected = allLogKindsSelected();
      document.querySelectorAll("[data-log-kind]").forEach(button => {
        const kind = button.dataset.logKind;
        button.classList.toggle("active", kind === "all" ? allSelected : logKindFilter.has(kind));
      });
    }

    function saveLogKindFilter() {
      localStorage.setItem(logKindStorageKey, allLogKindsSelected() ? "all" : logKindOptions.filter(kind => logKindFilter.has(kind)).join(","));
    }

    function setLogKindFilter(kind) {
      if (kind === "all") {
        logKindFilter = allLogKindsSelected() ? new Set() : new Set(logKindOptions);
      } else if (logKindOptions.includes(kind)) {
        if (logKindFilter.has(kind)) logKindFilter.delete(kind);
        else logKindFilter.add(kind);
      }
      saveLogKindFilter();
      updateLogKindButtons();
      replayThinkingKey = "";
      if (latestLearnerData) renderLearner(latestLearnerData);
      else renderBoardThinking(true);
    }

    function updateBoardOrientationButton() {
      const button = document.getElementById("flip-board");
      button.textContent = boardOrientation === "white" ? "Flip Board" : "White View";
      button.title = boardOrientation === "white" ? "Flip board to Black" : "Flip board to White";
    }

    function setBoardOrientation(orientation) {
      boardOrientation = orientation === "black" ? "black" : "white";
      localStorage.setItem("livePgnBoardOrientation", boardOrientation);
      updateBoardOrientationButton();
      if (latestGame && latestGame.has_game) renderGame(latestGame);
      else renderBoard("8/8/8/8/8/8/8/8 w - - 0 1", null);
    }

    function updateAnalysisControls() {
      for (const id of ["analysis-toggle", "analysis-panel-toggle"]) {
        const el = document.getElementById(id);
        el.checked = analysisAvailable && analysisEnabled;
        el.disabled = !analysisAvailable;
      }
    }

    function setAnalysisEnabled(enabled) {
      if (!analysisAvailable && enabled) {
        updateAnalysisControls();
        return;
      }
      analysisEnabled = enabled;
      localStorage.setItem("livePgnAnalysis", analysisEnabled ? "on" : "off");
      updateAnalysisControls();
      refresh();
    }

    function setActiveView(view) {
      activeView = view === "learner" ? "learner" : "board";
      document.getElementById("board-view").classList.toggle("hidden", activeView !== "board");
      document.getElementById("learner-view").classList.toggle("hidden", activeView !== "learner");
      document.getElementById("board-view-tab").classList.toggle("active", activeView === "board");
      document.getElementById("learner-view-tab").classList.toggle("active", activeView === "learner");
      localStorage.setItem("livePgnView", activeView);
      if (activeView === "learner") loadLearner();
    }

    function setActivePgnPath(path) {
      activePgnPath = path || "";
      document.getElementById("pgn-path").textContent = activePgnPath;
    }

    function setActiveMatchUrl(url) {
      activeMatchUrl = url || "";
      document.getElementById("copy-pgn-path").disabled = !activeMatchUrl;
    }

    function encodeMatchHash(slug, gameIndex = null, hashKind = "archive") {
      if (!slug) return "";
      const index = Number(gameIndex);
      const suffix = Number.isFinite(index) && index > 0
        ? (hashKind === "live" ? `--live-game-${index}` : `--game-${index}`)
        : "";
      return `#${encodeURIComponent(`${slug}${suffix}`)}`;
    }

    function matchUrlFor(slug, gameIndex = null, hashKind = "archive") {
      const hash = encodeMatchHash(slug, gameIndex, hashKind);
      if (!hash) return "";
      const url = new URL(window.location.href);
      url.hash = hash;
      return url.href;
    }

    function parseMatchHash() {
      const raw = decodeURIComponent((window.location.hash || "").replace(/^#/, "")).trim();
      if (!raw) return { slug: "", gameIndex: null, liveGameIndex: null };
      const liveMatch = raw.match(/^(.*)--live-game-(\\d+)$/);
      if (liveMatch) return { slug: liveMatch[1], gameIndex: null, liveGameIndex: Number(liveMatch[2]) || null };
      const match = raw.match(/^(.*)--game-(\\d+)$/);
      if (!match) return { slug: raw, gameIndex: null, liveGameIndex: null };
      return { slug: match[1], gameIndex: Number(match[2]) || null, liveGameIndex: null };
    }

    function setMatchHash(slug, gameIndex = null, hashKind = "archive") {
      const next = encodeMatchHash(slug, gameIndex, hashKind);
      if (!next || window.location.hash === next) return;
      suppressHashChange = true;
      window.history.replaceState(null, "", next);
      window.setTimeout(() => { suppressHashChange = false; }, 0);
    }

    function archiveGameHashIndex(data) {
      if (selectedMatch && !followLive) return data.game_index || selectedMatch.game_index || 1;
      if (!followLive && data && data.has_game) return data.game_index || 1;
      return null;
    }

    function liveMatchHashIndexForData(data) {
      const slug = data && data.tournament_slug ? data.tournament_slug : "";
      const path = data && data.path ? data.path : activeLivePgnPath;
      const boardMatch = previousMatches.find(match => {
        if (match.kind !== "live" || !match.is_board_game) return false;
        if (slug && (match.tournament_slug || "") !== slug) return false;
        return !path || !match.path || match.path === path;
      });
      if (boardMatch) return boardMatch.game_index || 1;
      return null;
    }

    function liveGameHashIndex(data) {
      if (!followLive) return null;
      if (requestedLiveGame !== null) return requestedLiveGame;
      return liveMatchHashIndexForData(data);
    }

    function replayLiveGameHashIndex(data) {
      if (followLive || selectedMatch || !data || !data.has_game) return null;
      const parsedHash = parseMatchHash();
      if (parsedHash.liveGameIndex && (!parsedHash.slug || parsedHash.slug === data.tournament_slug)) {
        return parsedHash.liveGameIndex;
      }
      if (requestedLiveGame !== null) return requestedLiveGame;
      return liveMatchHashIndexForData(data);
    }

    function syncMatchHash(data) {
      if (!data || !data.tournament_slug || !data.has_game) {
        setActiveMatchUrl("");
        return;
      }
      const parsedHash = parseMatchHash();
      if (!selectedMatch && parsedHash.slug && (parsedHash.gameIndex || parsedHash.liveGameIndex) && !previousMatches.length) {
        setActiveMatchUrl(window.location.href);
        return;
      }
      if (followLive) {
        const liveIndex = liveGameHashIndex(data);
        setActiveMatchUrl(matchUrlFor(data.tournament_slug, liveIndex, liveIndex ? "live" : "archive"));
        setMatchHash(data.tournament_slug, liveIndex, liveIndex ? "live" : "archive");
        return;
      }
      const replayLiveIndex = replayLiveGameHashIndex(data);
      if (replayLiveIndex) {
        setActiveMatchUrl(matchUrlFor(data.tournament_slug, replayLiveIndex, "live"));
        setMatchHash(data.tournament_slug, replayLiveIndex, "live");
        return;
      }
      const gameIndex = archiveGameHashIndex(data);
      setActiveMatchUrl(matchUrlFor(data.tournament_slug, gameIndex, "archive"));
      setMatchHash(data.tournament_slug, gameIndex, "archive");
    }

    function latestLiveMatch() {
      return previousMatches.find(match => match.kind === "live" && match.is_board_game)
        || previousMatches.find(match => match.kind === "live")
        || null;
    }

    function followLiveMatch(match) {
      if (!match) return false;
      selectedMatch = null;
      followLive = true;
      viewedPly = null;
      replayThinkingKey = "";
      requestedLiveGame = match.game_index || 1;
      activeLivePgnPath = match.path || activeLivePgnPath;
      localStorage.setItem("livePgnFollow", "on");
      document.getElementById("follow-toggle").checked = true;
      setMatchHash(match.tournament_slug || "", requestedLiveGame, "live");
      setActiveMatchUrl(matchUrlFor(match.tournament_slug || "", requestedLiveGame, "live"));
      renderPreviousMatches();
      selectLiveMatch(match);
      return true;
    }

    function maybeFollowLatestLiveForBareHash() {
      const parsed = parseMatchHash();
      if (!parsed.slug || parsed.gameIndex || parsed.liveGameIndex) return false;
      const match = latestLiveMatch();
      if (!match) return false;
      const matchSlug = match.tournament_slug || "";
      const currentLivePath = activeLivePgnPath || (followLive && !selectedMatch ? activePgnPath : "");
      const slugChanged = matchSlug !== parsed.slug;
      const pathChanged = !!(match.path && currentLivePath && match.path !== currentLivePath);
      const currentCompleted = !!(latestGame && latestGame.completed);
      if (!slugChanged && !pathChanged && !currentCompleted) return false;
      return followLiveMatch(match);
    }

    function maybeFollowCurrentLiveBoard() {
      if (!followLive || selectedMatch) return false;
      const match = latestLiveMatch();
      if (!match) return false;
      const pathChanged = !!(match.path && activeLivePgnPath && match.path !== activeLivePgnPath);
      const requestedChanged = requestedLiveGame !== null && Number(match.game_index || 1) !== Number(requestedLiveGame);
      const currentCompleted = !!(latestGame && latestGame.completed);
      if (!pathChanged && !requestedChanged && !currentCompleted) return false;
      return followLiveMatch(match);
    }

    function fallbackCopy(text) {
      const box = document.createElement("textarea");
      box.value = text;
      box.setAttribute("readonly", "");
      box.style.position = "fixed";
      box.style.left = "-9999px";
      document.body.appendChild(box);
      box.select();
      try {
        return document.execCommand("copy");
      } finally {
        document.body.removeChild(box);
      }
    }

    async function copyTextToClipboard(text, button) {
      if (!text) return;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
        } else if (!fallbackCopy(text)) {
          throw new Error("copy unavailable");
        }
        if (button) button.classList.add("copied");
        setStatus(true, "URL copied");
        if (button) setTimeout(() => button.classList.remove("copied"), 900);
      } catch {
        setStatus(false, "Copy failed");
      }
    }

    async function copyActiveMatchUrl() {
      await copyTextToClipboard(activeMatchUrl, document.getElementById("copy-pgn-path"));
    }

    async function copyMatchRowUrl(index, button) {
      const match = previousMatches[Number(index)];
      if (!match) return;
      const gameIndex = match.game_index || 1;
      const hashKind = match.kind === "live" ? "live" : "archive";
      await copyTextToClipboard(matchUrlFor(match.tournament_slug || "", gameIndex, hashKind), button);
    }

    function squareName(file, rank) {
      return "abcdefgh"[file] + String(8 - rank);
    }

    function parseFen(fen) {
      const boardPart = (fen || "8/8/8/8/8/8/8/8").split(" ")[0];
      const rows = boardPart.split("/");
      const pieces = {};
      rows.forEach((row, rank) => {
        let file = 0;
        for (const token of row) {
          if (/\\d/.test(token)) {
            file += Number(token);
          } else {
            pieces[squareName(file, rank)] = token;
            file += 1;
          }
        }
      });
      return pieces;
    }

    function renderBoard(fen, lastMove) {
      const boardEl = document.getElementById("board");
      const pieces = parseFen(fen);
      boardEl.innerHTML = "";
      for (let rank = 0; rank < 8; rank++) {
        for (let file = 0; file < 8; file++) {
          const displayRank = boardOrientation === "black" ? 7 - rank : rank;
          const displayFile = boardOrientation === "black" ? 7 - file : file;
          const sq = squareName(displayFile, displayRank);
          const isLight = (displayRank + displayFile) % 2 === 0;
          const isLast = lastMove && (sq === lastMove.from || sq === lastMove.to);
          const el = document.createElement("div");
          el.className = "sq " + (isLight ? "light" : "dark") + (isLast ? " last" : "");
          const piece = pieces[sq];
          if (piece) {
            const key = PIECE_KEY[piece];
            if (key && PIECES[key]) {
              const span = document.createElement("span");
              span.className = "piece";
              span.innerHTML = PIECES[key];
              el.appendChild(span);
            }
          }
          if (file === 0) {
            const r = document.createElement("span");
            r.className = "coord rank";
            r.textContent = String(8 - displayRank);
            el.appendChild(r);
          }
          if (rank === 7) {
            const f = document.createElement("span");
            f.className = "coord file";
            f.textContent = "abcdefgh"[displayFile];
            el.appendChild(f);
          }
          boardEl.appendChild(el);
        }
      }
    }

    function renderMoves(moves, whiteName = "White", blackName = "Black") {
      const container = document.getElementById("moves");
      if (!moves.length) {
        container.innerHTML = '<div class="card-body"><div class="empty">No moves yet.</div></div>';
        return;
      }
      const rows = [];
      for (let i = 0; i < moves.length; i += 2) {
        const w = moves[i];
        const b = moves[i + 1];
        rows.push(`<tr><td>${escapeHtml(w.move_number)}</td><td>${escapeHtml(w.san)}</td><td>${b ? escapeHtml(b.san) : ""}</td></tr>`);
      }
      container.innerHTML = `<table class="moves-tbl"><thead><tr><th>#</th><th>White<br><span style="color:var(--muted);font-size:10px;text-transform:none;letter-spacing:0;font-weight:400">${escapeHtml(whiteName)}</span></th><th>Black<br><span style="color:var(--muted);font-size:10px;text-transform:none;letter-spacing:0;font-weight:400">${escapeHtml(blackName)}</span></th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
    }

    function selectedPly(data) {
      const maxPly = (data.moves || []).length;
      if (followLive || viewedPly === null) return maxPly;
      return Math.max(0, Math.min(viewedPly, maxPly));
    }

    function moveNumberLabel(move) {
      if (!move) return "start position";
      const suffix = move.side === "Black" ? "..." : ".";
      return `Move ${move.move_number}${suffix} ${move.side} ${move.san || move.uci}`;
    }

    function fenPrefix(fen) {
      return String(fen || "").split(" ").slice(0, 4).join(" ");
    }

    function logFen(text) {
      const value = String(text || "");
      const match = value.match(/\\bfen=(.*?)\\s+(?:legal_moves=|go=)/i) || value.match(/\\bfrom fen=(.*?)\\s+go=/i);
      return match ? fenPrefix(match[1]) : "";
    }

    function logMove(text) {
      const value = String(text || "");
      const match = value.match(/\\b(?:bestmove|move)=([a-h][1-8][a-h][1-8][qrbn]?|0000)\\b/i);
      return match ? match[1].toLowerCase() : "";
    }

    function logsForBoardPly(logs) {
      if (!logs || !logs.length) return { logs: [], label: "No bot decisions logged yet." };
      if (!latestGame || !latestGame.has_game || followLive) {
        const liveMove = latestGame && latestGame.moves ? latestGame.moves[Math.max(0, latestGame.moves.length - 1)] : null;
        const liveMoveText = liveMove ? ` · current ${moveNumberLabel(liveMove)}` : "";
        return { logs: logs.slice(0, 36), label: `${filterLogs(logs.slice(0, 36)).length} / ${Math.min(logs.length, 36)} recent entries${liveMoveText}` };
      }
      const ply = selectedPly(latestGame);
      const moves = latestGame.moves || [];
      const positions = latestGame.positions || [];
      const move = moves[ply - 1] || null;
      const before = positions.find(item => item.ply === Math.max(0, ply - 1));
      const targetFen = before ? fenPrefix(before.fen) : "";
      const targetMove = move ? String(move.uci || "").toLowerCase() : "";
      const targetSide = move ? String(move.side || "").toLowerCase() : "";
      const matched = logs.filter(entry => {
        const text = entry.text || "";
        const entryFen = logFen(text);
        const entryMove = logMove(text);
        const entrySide = String(entry.side || "").toLowerCase();
        if (entryMove && targetMove && entryMove === targetMove) return true;
        if (entryFen && targetFen && entryFen === targetFen && (!targetSide || !entrySide || entrySide === targetSide)) return true;
        return false;
      }).sort((a, b) => String(a.timestamp || "").localeCompare(String(b.timestamp || "")));
      const moveLabel = moveNumberLabel(move);
      return {
        logs: matched,
        label: `${filterLogs(matched).length} entries for ${moveLabel} (ply ${ply})`,
      };
    }

    function boardThinkingKey() {
      if (followLive || !latestGame || !latestGame.has_game) return "live";
      return [
        latestGame.path || "",
        latestGame.game_index || 0,
        selectedPly(latestGame),
        logSideFilter,
        logKindOptions.filter(kind => logKindFilter.has(kind)).join(","),
      ].join("|");
    }

    function renderBoardThinking(force = false) {
      const key = boardThinkingKey();
      if (!followLive && !force && replayThinkingKey === key) return;
      const logs = latestGame && Array.isArray(latestGame.logs)
        ? latestGame.logs
        : (latestLearnerData && Array.isArray(latestLearnerData.logs) ? latestLearnerData.logs : []);
      const selected = logsForBoardPly(logs);
      document.getElementById("board-thinking-meta").textContent = selected.label;
      document.getElementById("board-thinking-logs").innerHTML = logListHtml(selected.logs, "No bot decisions matched this replay move.");
      replayThinkingKey = followLive ? "" : key;
    }

    function formatClock(ms) {
      if (!Number.isFinite(ms)) return "--:--";
      const clamped = Math.max(0, Math.floor(ms));
      if (clamped < 10000) return (clamped / 1000).toFixed(1);
      const totalSeconds = Math.ceil(clamped / 1000);
      const hours = Math.floor(totalSeconds / 3600);
      const minutes = Math.floor((totalSeconds % 3600) / 60);
      const seconds = totalSeconds % 60;
      if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
      return `${minutes}:${String(seconds).padStart(2, "0")}`;
    }

    function clockValue(clock, side) {
      if (!clock) return NaN;
      const raw = side === "White" ? clock.white_ms : clock.black_ms;
      if (raw === null || raw === undefined || raw === "") return NaN;
      const base = Number(raw);
      if (!Number.isFinite(base)) return NaN;
      if (clock.completed || clock.running_side !== side || !Number.isFinite(Number(clock.updated_at_epoch_ms))) return base;
      const serverNow = Date.now() + serverClockOffsetMs;
      return base - Math.max(0, serverNow - Number(clock.updated_at_epoch_ms));
    }

    function updateClockElement(id, side) {
      const el = document.getElementById(id);
      if (!el) return;
      const value = clockValue(latestClock, side);
      el.textContent = formatClock(value);
      el.className = "clock";
      if (latestClock && latestClock.running_side === side && !latestClock.completed && (!Number.isFinite(value) || value > 0)) el.classList.add("running");
      if (Number.isFinite(value) && value <= 30000) el.classList.add("low");
      if (Number.isFinite(value) && value <= 10000) el.classList.add("danger");
    }

    function updateClockDisplays() {
      updateClockElement("black-clock", "Black");
      updateClockElement("white-clock", "White");
    }

    let viewerVersion = "";
    async function checkViewerVersion() {
      try {
        const resp = await fetch("/api/viewer-version", { cache: "no-store" });
        const data = await resp.json();
        if (!data.hot_reload || !data.version) return;
        if (!viewerVersion) {
          viewerVersion = data.version;
          return;
        }
        if (data.version !== viewerVersion) window.location.reload();
      } catch {
        // The hot-reload supervisor may be between child processes.
      }
    }

    function renderClock(data) {
      latestClock = data.clock || null;
      if (latestClock && Number.isFinite(Number(latestClock.server_now_epoch_ms))) {
        serverClockOffsetMs = Number(latestClock.server_now_epoch_ms) - Date.now();
      }
      updateClockDisplays();
    }

    function playerBarHtml(side, name) {
      const isWhite = side === "White";
      return `<span class="${isWhite ? "dot-w" : "dot-b"}"></span><span class="bar-name">${side}: ${escapeHtml(name)}</span><span id="${isWhite ? "white-clock" : "black-clock"}" class="clock">--:--</span>`;
    }

    function renderPlayerBars(white, black) {
      if (boardOrientation === "black") {
        document.getElementById("top-player").innerHTML = playerBarHtml("White", white);
        document.getElementById("bottom-player").innerHTML = playerBarHtml("Black", black);
      } else {
        document.getElementById("top-player").innerHTML = playerBarHtml("Black", black);
        document.getElementById("bottom-player").innerHTML = playerBarHtml("White", white);
      }
      updateClockDisplays();
    }

    function formatGameResult(headers) {
      const result = headers.Result || "*";
      const reason = String(headers.Termination || "").trim();
      const suffix = reason && reason.toLowerCase() !== "normal" ? ` - ${reason}` : "";
      if (result === "1-0") return `${headers.White || "White"} (White) won${suffix}`;
      if (result === "0-1") return `${headers.Black || "Black"} (Black) won${suffix}`;
      if (result === "1/2-1/2") return `Draw${suffix}`;
      return result;
    }

    function currentGameLabel(data, white, black) {
      const headers = data.headers || {};
      const gameNumber = Number(headers.Round || data.game_index || 1);
      const totalGames = Number(headers.TotalGames || data.total_games || 0);
      const gameText = Number.isFinite(gameNumber) && gameNumber > 0
        ? `Game ${gameNumber}${Number.isFinite(totalGames) && totalGames > 0 ? ` / ${totalGames}` : ""}`
        : "Current game";
      return `${gameText}: ${white} vs ${black}`;
    }

    function liveClockTimeoutResult(data, white, black) {
      if (!data || data.completed || !latestClock || !latestClock.running_side) return "";
      const side = latestClock.running_side;
      const value = clockValue(latestClock, side);
      if (!Number.isFinite(value) || value > 0) return "";
      if (side === "White") return `${black || "Black"} (Black) won - ${white || "White"} (White) lost on time`;
      if (side === "Black") return `${white || "White"} (White) won - ${black || "Black"} (Black) lost on time`;
      return "";
    }

    function renderGame(data) {
      latestGame = data;
      const headers = data.headers || {};
      const white = headers.White || "White";
      const black = headers.Black || "Black";
      const ply = selectedPly(data);
      const positions = data.positions || [];
      const position = positions.find(item => item.ply === ply) || {
        ply, fen: data.fen, last_move: data.last_move, turn: data.turn
      };
      viewedPly = ply;

      document.getElementById("players").textContent = `${white} vs ${black}`;
      document.getElementById("current-game-title").textContent = currentGameLabel(data, white, black);
      document.getElementById("tournament-chip").textContent = `Tournament: ${data.tournament_slug || "—"}`;
      document.getElementById("white-player").innerHTML = `<span class="dot-w"></span>White: ${escapeHtml(white)}`;
      document.getElementById("black-player").innerHTML = `<span class="dot-b"></span>Black: ${escapeHtml(black)}`;
      renderPlayerBars(white, black);
      renderClock(data);
      const timeoutResult = liveClockTimeoutResult(data, white, black);
      document.getElementById("turn").textContent = followLive
        ? ((data.completed || timeoutResult) ? "Game over" : `${data.turn} to move`)
        : `${ply} / ${data.moves.length} plies`;
      document.getElementById("result").textContent = timeoutResult || formatGameResult(headers);
      const gameLabel = data.game_count > 1 ? `game ${data.game_index} / ${data.game_count}, ` : "";
      document.getElementById("meta").textContent = followLive
        ? `${gameLabel}${data.moves.length} plies`
        : `${gameLabel}ply ${ply}`;
      document.getElementById("prev-move").disabled = ply <= 0;
      document.getElementById("next-move").disabled = ply >= data.moves.length;
      renderBoard(position.fen, position.last_move);
      renderMoves(data.moves, white, black);
      renderAnalysis(analysisEnabled ? data.analysis : { enabled: false }, analysisEnabled);
      renderBoardThinking();
    }

    function setFollowLive(enabled) {
      followLive = enabled;
      localStorage.setItem("livePgnFollow", followLive ? "on" : "off");
      document.getElementById("follow-toggle").checked = followLive;
      replayThinkingKey = "";
      if (followLive) selectedMatch = null;
      if (followLive && latestGame) viewedPly = latestGame.moves.length;
      refresh(true);
    }

    function navigateMove(delta) {
      if (!latestGame || !latestGame.has_game) return;
      const maxPly = latestGame.moves.length;
      const current = selectedPly(latestGame);
      viewedPly = Math.max(0, Math.min(current + delta, maxPly));
      followLive = false;
      localStorage.setItem("livePgnFollow", "off");
      document.getElementById("follow-toggle").checked = false;
      replayThinkingKey = "";
      renderGame(latestGame);
      syncMatchHash(latestGame);
      refresh(true);
    }

    function replayLivePgnPath() {
      if (selectedMatch || followLive) return "";
      if (latestGame && latestGame.path) return latestGame.path;
      return activeLivePgnPath || activePgnPath || "";
    }

    function renderAnalysis(analysis) {
      const container = document.getElementById("analysis");
      const meta = document.getElementById("analysis-meta");
      const title = document.getElementById("analysis-title");
      title.textContent = analysis && analysis.engine ? `Engine Analysis: ${analysis.engine}` : "Engine Analysis";
      if (!analysis || analysis.enabled === false) {
        if (!analysisAvailable) {
          updateAnalysisControls();
          meta.textContent = "Unavailable";
          container.innerHTML = '<div class="empty">Analysis is unavailable. Start the viewer with Stockfish analysis enabled.</div>';
          return;
        }
        if (analysisEnabled) {
          analysisAvailable = false;
          analysisEnabled = false;
          localStorage.setItem("livePgnAnalysis", "off");
          updateAnalysisControls();
          meta.textContent = "Unavailable";
          container.innerHTML = '<div class="empty">Analysis is unavailable. Start the viewer with Stockfish analysis enabled.</div>';
          return;
        }
        updateAnalysisControls();
        meta.textContent = "";
        container.innerHTML = '<div class="empty">Analysis is off.</div>';
        return;
      }
      analysisAvailable = true;
      updateAnalysisControls();
      if (analysis.error) {
        meta.textContent = "";
        container.innerHTML = `<div class="empty err">${escapeHtml(analysis.error)}</div>`;
        return;
      }
      if (analysis.message) {
        meta.textContent = "";
        container.innerHTML = `<div class="empty">${escapeHtml(analysis.message)}</div>`;
        return;
      }
      const lines = analysis.lines || [];
      meta.textContent = analysis.engine ? `${analysis.engine} · ${analysis.movetime_ms} ms` : "";
      if (!lines.length) {
        container.innerHTML = '<div class="empty">No analysis available.</div>';
        return;
      }
      container.innerHTML = `<div class="analysis-list">${lines.map(line => `
        <div class="analysis-line">
          <div class="score">${escapeHtml(line.score)}</div>
          <div class="pv">${escapeHtml(line.pv_san || line.pv_uci || "")}<div class="pv-meta">depth ${escapeHtml(line.depth || "—")}${line.bestmove ? " · best " + escapeHtml(line.bestmove) : ""}</div></div>
        </div>`).join("")}</div>`;
    }

    function renderStats(data) {
      const container = document.getElementById("stats");
      document.getElementById("stats-meta").textContent = `${data.games} games`;
      previousMatches = data.matches || [];
      if (!maybeFollowLatestLiveForBareHash() && !maybeFollowCurrentLiveBoard()) {
        selectPreviousMatchFromHash();
      }
      renderPreviousMatches();
      if (data.error) {
        container.innerHTML = `<div class="empty err">${escapeHtml(data.error)}</div>`;
        return;
      }
      if (!data.engines.length) {
        container.innerHTML = '<div class="empty">No completed games found.</div>';
        return;
      }
      const rows = data.engines.map((eng, i) => `
        <tr>
          <td class="rk">${i + 1}</td>
          <td>${escapeHtml(eng.engine)}</td>
          <td class="n">${escapeHtml(eng.points)}</td>
          <td class="n">${escapeHtml(eng.wins)}</td>
          <td class="n">${escapeHtml(eng.draws)}</td>
          <td class="n">${escapeHtml(eng.losses)}</td>
          <td class="n">${escapeHtml(eng.games)}</td>
        </tr>`);
      container.innerHTML = `<table class="stats-tbl"><thead><tr><th class="rk">#</th><th>Engine</th><th class="n">Pts</th><th class="n">W</th><th class="n">D</th><th class="n">L</th><th class="n">G</th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
    }

    function renderPreviousMatches() {
      const container = document.getElementById("matches");
      const meta = document.getElementById("matches-meta");
      const total = previousMatches.length;
      const pages = Math.max(1, Math.ceil(total / previousMatchesPageSize));
      previousMatchesPage = Math.max(0, Math.min(previousMatchesPage, pages - 1));
      const start = previousMatchesPage * previousMatchesPageSize;
      const pageRows = previousMatches.slice(start, start + previousMatchesPageSize);
      const activeKey = selectedMatch ? matchKey(selectedMatch) : "";
      meta.textContent = total ? `${previousMatchesPage + 1} / ${pages}` : "0 / 0";
      document.getElementById("matches-prev").disabled = previousMatchesPage <= 0;
      document.getElementById("matches-next").disabled = previousMatchesPage >= pages - 1;
      if (!total) {
        container.innerHTML = '<div class="empty">No previous matches found.</div>';
        return;
      }
      container.innerHTML = `<div class="match-list">${pageRows.map(match => {
        const index = previousMatches.indexOf(match);
        const isRequestedLive = match.kind === "live" && requestedLiveGame !== null && Number(match.game_index || 1) === Number(requestedLiveGame) && (!activeLivePgnPath || !match.path || match.path === activeLivePgnPath);
        const isActive = matchKey(match) === activeKey || isRequestedLive || (match.kind === "live" && requestedLiveGame === null && !selectedMatch && followLive && match.is_board_game);
        const isLive = match.kind === "live";
        const clickable = true;
        const rowClasses = ["match-row", isActive ? "active" : "", isLive ? "in-progress" : "", clickable ? "" : "disabled"].filter(Boolean).join(" ");
        const statusClass = isLive ? "in-progress" : "";
        const statusLabel = match.status_label || match.result || "";
        const gameLabel = isLive && match.total ? `game ${match.game_index || 1} / ${match.total}` : `game ${match.game_index || 1}`;
        const fileLabel = match.file ? ` · ${escapeHtml(match.file)}` : "";
        return `<div class="${rowClasses}">
          <button class="match-select" type="button" ${clickable ? `data-match-index="${escapeAttr(index)}"` : "disabled"}>
            <div class="match-main"><span>${escapeHtml(match.date || match.updated_at || "live")}</span><span class="status-badge ${statusClass}">${escapeHtml(statusLabel)}</span></div>
            <div class="match-sub"><span class="match-winner">${escapeHtml(match.winner_label)}</span> · ${escapeHtml(match.white)} vs ${escapeHtml(match.black)} · ${escapeHtml(match.tournament_slug || "unknown")} · ${escapeHtml(gameLabel)}${fileLabel}</div>
          </button>
          <button class="match-copy-btn" type="button" title="Copy match URL" aria-label="Copy match URL" data-match-copy-index="${escapeAttr(index)}">&#10697;</button>
        </div>`;
      }).join("")}</div>`;
    }

    function setPreviousMatchesPage(delta) {
      previousMatchesPage += delta;
      renderPreviousMatches();
    }

    function matchKey(match) {
      if (!match) return "";
      return `${match.kind || "completed"}|${match.path || match.file || ""}|${match.game_index || 1}`;
    }

    function loadedGameKey(data) {
      if (!data || !data.path) return "";
      return `${data.path}|${data.game_index || 1}`;
    }

    function selectedGameKey(match) {
      if (!match) return "";
      return `${match.path || match.file || ""}|${match.game_index || 1}`;
    }

    function selectedMatchIsLoaded(match, data) {
      return !!match && !!data && loadedGameKey(data) === selectedGameKey(match);
    }

    function loadPreviousMatch(index) {
      const match = previousMatches[Number(index)];
      if (!match) return;
      if (match.kind === "live") {
        followLiveMatch(match);
        return;
      }
      selectedMatch = match;
      followLive = false;
      viewedPly = null;
      replayThinkingKey = "";
      localStorage.setItem("livePgnFollow", "off");
      document.getElementById("follow-toggle").checked = false;
      setMatchHash(match.tournament_slug || "", match.game_index || 1);
      setActiveMatchUrl(matchUrlFor(match.tournament_slug || "", match.game_index || 1));
      renderPreviousMatches();
      refresh(true);
    }

    async function selectLiveMatch(match) {
      activeLivePgnPath = match.path || activeLivePgnPath || activePgnPath;
      const controlPath = match.control_path || activeLivePgnPath || activePgnPath;
      try {
        const resp = await fetch("/api/live-selection", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: controlPath, game: match.game_index || 1 }),
        });
        if (!resp.ok) throw new Error("selection failed");
        setStatus(true, `Following game ${match.game_index || 1}`);
      } catch {
        setStatus(false, "Live switch failed");
      }
      renderPreviousMatches();
      await loadStats();
      window.setTimeout(() => refresh(true), 350);
    }

    function selectPreviousMatchFromHash() {
      const parsed = parseMatchHash();
      if (!parsed.slug || !previousMatches.length) return false;
      if (parsed.liveGameIndex) {
        const liveMatch = previousMatches.find(item => {
          if (item.kind !== "live") return false;
          if ((item.tournament_slug || "") !== parsed.slug) return false;
          return Number(item.game_index || 1) === parsed.liveGameIndex;
        });
        if (!liveMatch) return false;
        if (!followLive && !selectedMatch) {
          const currentPath = (latestGame && latestGame.path) || activeLivePgnPath || "";
          const sameLiveGame = requestedLiveGame !== null
            && Number(requestedLiveGame) === Number(liveMatch.game_index || 1)
            && (!currentPath || !liveMatch.path || currentPath === liveMatch.path);
          if (sameLiveGame) {
            requestedLiveGame = liveMatch.game_index || requestedLiveGame;
            activeLivePgnPath = liveMatch.path || activeLivePgnPath;
            setMatchHash(liveMatch.tournament_slug || "", requestedLiveGame, "live");
            setActiveMatchUrl(matchUrlFor(liveMatch.tournament_slug || "", requestedLiveGame, "live"));
            return true;
          }
        }
        const alreadyFollowing = followLive
          && !selectedMatch
          && requestedLiveGame !== null
          && Number(requestedLiveGame) === Number(liveMatch.game_index || 1)
          && (!activeLivePgnPath || !liveMatch.path || activeLivePgnPath === liveMatch.path);
        if (alreadyFollowing) return true;
        return followLiveMatch(liveMatch);
      }
      if (!parsed.gameIndex) return false;
      const match = previousMatches.find(item => {
        if (item.kind === "live") return false;
        if ((item.tournament_slug || "") !== parsed.slug) return false;
        return parsed.gameIndex ? Number(item.game_index || 1) === parsed.gameIndex : true;
      });
      if (!match) return false;
      if (selectedMatch && matchKey(selectedMatch) === matchKey(match)) return true;
      selectedMatch = match;
      followLive = false;
      viewedPly = null;
      replayThinkingKey = "";
      localStorage.setItem("livePgnFollow", "off");
      document.getElementById("follow-toggle").checked = false;
      return true;
    }

    function fileListHtml(files) {
      if (!files || !files.length) return '<div class="empty">No files yet.</div>';
      return `<div class="file-list">${files.map(file => `
        <details class="file-row">
          <summary><span>${escapeHtml(file.name)}</span><span class="file-meta">${escapeHtml(file.size_label)} · ${escapeHtml(file.updated_at || "")}</span></summary>
          <pre class="doc-text">${escapeHtml(file.text || "")}</pre>
        </details>`).join("")}</div>`;
    }

    function filterLogsBySide(logs) {
      if (!logs || logSideFilter === "all") return logs || [];
      return logs.filter(entry => String(entry.side || "").toLowerCase() === logSideFilter);
    }

    function filterLogsByKind(logs) {
      if (!logs || allLogKindsSelected()) return logs || [];
      if (!logKindFilter.size) return [];
      return logs.filter(entry => logKindFilter.has(String(entry.kind || "log").toLowerCase()));
    }

    function filterLogs(logs) {
      return filterLogsByKind(filterLogsBySide(logs));
    }

    function logListHtml(logs, emptyText = "No bot log entries yet.") {
      const visibleLogs = filterLogs(logs);
      if (!visibleLogs || !visibleLogs.length) return `<div class="empty">${escapeHtml(emptyText)}</div>`;
      return `<div class="log-list">${visibleLogs.map(entry => {
        const bot = entry.bot || "unknown";
        const kind = entry.kind || "log";
        const side = String(entry.side || "").toLowerCase();
        const sideChip = side ? `<span class="log-side ${escapeAttr(side)}">${escapeHtml(side)}</span>` : "";
        return `<div class="log-entry ${escapeAttr(bot)} ${escapeAttr(side)} log-kind-${escapeAttr(kind)}">
          <div class="log-head">
            <span><span class="bot-name">${escapeHtml(entry.bot_label || bot)}</span><span class="log-kind">${escapeHtml(kind)}</span>${sideChip}</span>
            <span>${escapeHtml(entry.timestamp || "")}</span>
          </div>
          <div class="log-text">${escapeHtml(entry.text || "")}</div>
        </div>`;
      }).join("")}</div>`;
    }

    function renderLearner(data) {
      latestLearnerData = data;
      document.getElementById("learner-updated").textContent = data.updated_at || "";
      if (data.error) {
        document.getElementById("learner-summary").innerHTML = `<div class="empty err">${escapeHtml(data.error)}</div>`;
        if (!latestGame || !Array.isArray(latestGame.logs)) {
          document.getElementById("board-thinking-meta").textContent = "error";
          document.getElementById("board-thinking-logs").innerHTML = `<div class="empty err">${escapeHtml(data.error)}</div>`;
        }
        return;
      }
      const summary = data.summary || {};
      document.getElementById("learner-summary").innerHTML = `<div class="learner-summary">
        <div class="summary-row"><span>Context</span><strong>${escapeHtml(data.root || "")}</strong></div>
        <div class="summary-row"><span>Memory</span><strong>${escapeHtml(summary.memory || "missing")}</strong></div>
        <div class="summary-row"><span>Knowledgebase</span><strong>${escapeHtml(summary.knowledgebase_files ?? 0)} files</strong></div>
        <div class="summary-row"><span>Skills</span><strong>${escapeHtml(summary.skill_files ?? 0)} files</strong></div>
        <div class="summary-row"><span>Learner logs</span><strong>${escapeHtml(summary.learner_logs ?? 0)} files</strong></div>
      </div>`;

      const memory = data.memory || {};
      document.getElementById("learner-memory-meta").textContent = memory.exists ? `${memory.size_label} · ${memory.updated_at}` : "missing";
      document.getElementById("learner-memory-text").textContent = memory.exists ? (memory.text || "") : "No learner memory file found.";

      const kb = data.knowledgebase || [];
      document.getElementById("learner-kb-meta").textContent = `${kb.length} files`;
      document.getElementById("learner-kb").innerHTML = fileListHtml(kb);

      const skills = data.skills || [];
      document.getElementById("learner-skills-meta").textContent = `${skills.length} files`;
      document.getElementById("learner-skills").innerHTML = fileListHtml(skills);

      const logs = data.logs || [];
      const filteredLogs = filterLogs(logs);
      document.getElementById("learner-logs-meta").textContent = `${filteredLogs.length} / ${logs.length} entries`;
      document.getElementById("learner-logs").innerHTML = logListHtml(logs, "No learner log entries yet.");
      renderBoardThinking();
    }

    async function loadLearner() {
      const resp = await fetch("/api/learner", { cache: "no-store" });
      renderLearner(await resp.json());
    }

    function setStatus(ok, text) {
      document.getElementById("status-dot").className = "pulse" + (ok ? " ok" : "");
      document.getElementById("status-text").textContent = text;
    }

    async function refresh(force = false) {
      if (!force && selectedMatch && !followLive && selectedMatchIsLoaded(selectedMatch, latestGame)) return;
      try {
        const params = new URLSearchParams({ analysis: analysisEnabled ? "1" : "0" });
        const expectedSelection = selectedMatch && !followLive ? selectedGameKey(selectedMatch) : "";
        const expectedMode = followLive ? "live" : (selectedMatch ? "selected" : "replay");
        const expectedReplayPath = expectedMode === "replay" ? replayLivePgnPath() : "";
        if (selectedMatch && !followLive) {
          params.set("path", selectedMatch.path || selectedMatch.file || "");
          params.set("game", String(selectedMatch.game_index || 1));
          params.set("logs", "1");
        } else if (!followLive) {
          const replayPath = expectedReplayPath;
          if (replayPath) params.set("path", replayPath);
        } else if (followLive && activeLivePgnPath) {
          params.set("path", activeLivePgnPath);
        }
        if (!followLive && viewedPly !== null) params.set("ply", String(viewedPly));
        const resp = await fetch(`/api/game?${params}`, { cache: "no-store" });
        const data = await resp.json();
        if (expectedMode === "live" && !followLive) return;
        if (expectedMode === "replay" && (followLive || selectedMatch || replayLivePgnPath() !== expectedReplayPath)) return;
        if (expectedSelection && (followLive || expectedSelection !== selectedGameKey(selectedMatch))) return;
        if (expectedSelection && loadedGameKey(data) !== expectedSelection) return;
        if (followLive && activeLivePgnPath && data.path && data.path !== activeLivePgnPath) return;
        setActivePgnPath(data.path || "");
        if (followLive && data.path) activeLivePgnPath = data.path;
        if (data.error) {
          setStatus(false, "Error");
          document.getElementById("moves").innerHTML = `<div class="card-body"><div class="empty err">${escapeHtml(data.error)}</div></div>`;
          renderAnalysis(analysisEnabled ? data.analysis : { enabled: false });
          return;
        }
        if (!data.exists || !data.has_game) {
          latestGame = data;
          syncMatchHash(data);
          setStatus(false, data.exists ? "No game" : "No PGN");
          document.getElementById("players").textContent = "No game loaded";
          document.getElementById("current-game-title").textContent = "No game loaded";
          document.getElementById("tournament-chip").textContent = `Tournament: ${data.tournament_slug || "—"}`;
          document.getElementById("white-player").innerHTML = '<span class="dot-w"></span>White: —';
          document.getElementById("black-player").innerHTML = '<span class="dot-b"></span>Black: —';
          latestClock = null;
          renderPlayerBars("—", "—");
          document.getElementById("turn").textContent = "—";
          document.getElementById("result").textContent = "*";
          document.getElementById("meta").textContent = "";
          renderBoard("8/8/8/8/8/8/8/8 w - - 0 1", null);
          renderMoves([]);
          renderAnalysis(analysisEnabled ? data.analysis : { enabled: false });
          return;
        }
        if (followLive || viewedPly === null) viewedPly = data.moves.length;
        renderGame(data);
        syncMatchHash(data);
        const changed = data.fen !== lastFen;
        lastFen = data.fen;
        setStatus(true, selectedMatch && !followLive ? "Archive" : (changed ? "Updated" : "Watching"));
      } catch {
        setStatus(false, "Disconnected");
      }
    }

    async function loadStats() {
      const params = new URLSearchParams({ live: activeLivePgnPath || activePgnPath || "" });
      const resp = await fetch(`/api/stats?${params}`, { cache: "no-store" });
      renderStats(await resp.json());
    }

    async function loadConfig() {
      const resp = await fetch("/api/config", { cache: "no-store" });
      const data = await resp.json();
      document.getElementById("config-path").textContent = data.path || "";
      document.getElementById("config-json").value = data.text || "";
      document.getElementById("config-status").textContent = data.exists ? `${data.engines || 0} engines` : "Not found";
      document.getElementById("config-status").className = data.exists ? "card-sub" : "card-sub err";
      try {
        configData = data.text ? JSON.parse(data.text) : [];
        renderConfigControls();
      } catch (err) {
        configData = [];
        document.getElementById("config-controls").innerHTML = `<div class="empty err">${escapeHtml(err.message)}</div>`;
      }
      syncConfigMode();
    }

    function settingControl(ei, si, setting) {
      const v = setting.value;
      const base = `data-engine="${ei}" data-setting="${si}" data-field="setting"`;
      if (typeof v === "boolean") return `<label>${escapeHtml(setting.name)}<input ${base} type="checkbox" ${v ? "checked" : ""}></label>`;
      if (typeof v === "number") return `<label>${escapeHtml(setting.name)}<input ${base} type="number" value="${escapeAttr(v)}"></label>`;
      return `<label>${escapeHtml(setting.name)}<input ${base} type="text" value="${escapeAttr(v)}"></label>`;
    }

    function renderConfigControls() {
      const container = document.getElementById("config-controls");
      if (!Array.isArray(configData) || !configData.length) {
        container.innerHTML = '<div class="empty">No engines configured.</div>';
        return;
      }
      container.innerHTML = configData.map((engine, ei) => {
        const settings = Array.isArray(engine.settings) ? engine.settings : [];
        const go = engine.go || {};
        return `<div class="eng-card">
          <div class="eng-head">
            <label>Name<input data-engine="${ei}" data-field="name" type="text" value="${escapeAttr(engine.name || "")}"></label>
            <label>Enabled<input data-engine="${ei}" data-field="enabled" type="checkbox" ${engine.enabled ? "checked" : ""}></label>
            <label>Elo<input data-engine="${ei}" data-field="elo" type="number" value="${escapeAttr(engine.elo ?? "")}"></label>
          </div>
          <div class="eng-fields">
            <label>Version<input data-engine="${ei}" data-field="version" type="text" value="${escapeAttr(engine.version || "")}"></label>
            <label>Go type<select data-engine="${ei}" data-field="goType">${["Infinite","Time","Depth","Nodes"].map(o => `<option value="${o}" ${go.t === o ? "selected" : ""}>${o}</option>`).join("")}</select></label>
            <label class="full-col">Path<input data-engine="${ei}" data-field="path" type="text" value="${escapeAttr(engine.path || "")}"></label>
            <label>Go value<input data-engine="${ei}" data-field="goControl" type="number" value="${escapeAttr(go.c ?? "")}"></label>
          </div>
          <div class="eng-settings">
            <div class="eng-settings-title">Settings</div>
            ${settings.length ? settings.map((s, si) => settingControl(ei, si, s)).join("") : '<div class="empty" style="padding:4px 0">None</div>'}
          </div>
        </div>`;
      }).join("");
    }

    function coerceNumber(value) {
      if (value === "") return null;
      const p = Number(value);
      return Number.isFinite(p) ? p : value;
    }

    function updateConfigFromInput(input) {
      const ei = Number(input.dataset.engine);
      const engine = configData[ei];
      if (!engine) return;
      const field = input.dataset.field;
      if (field === "enabled") {
        engine.enabled = input.checked;
      } else if (field === "elo") {
        const p = coerceNumber(input.value);
        if (p === null) delete engine.elo; else engine.elo = p;
      } else if (field === "goType") {
        engine.go = engine.go || {}; engine.go.t = input.value;
      } else if (field === "goControl") {
        engine.go = engine.go || {};
        const p = coerceNumber(input.value);
        if (p === null) delete engine.go.c; else engine.go.c = p;
      } else if (field === "setting") {
        const si = Number(input.dataset.setting);
        const s = Array.isArray(engine.settings) ? engine.settings[si] : null;
        if (!s) return;
        if (input.type === "checkbox") s.value = input.checked;
        else if (input.type === "number") { const p = coerceNumber(input.value); s.value = p === null ? 0 : p; }
        else s.value = input.value;
      } else {
        engine[field] = input.value;
      }
    }

    function syncConfigMode() {
      document.getElementById("raw-config-toggle").checked = rawConfigMode;
      document.getElementById("config-controls").classList.toggle("hidden", rawConfigMode);
      document.getElementById("raw-config-wrap").classList.toggle("hidden", !rawConfigMode);
      if (rawConfigMode) document.getElementById("config-json").value = JSON.stringify(configData, null, 4);
    }

    function setRawConfigMode(enabled) {
      if (!enabled && rawConfigMode) {
        try {
          configData = JSON.parse(document.getElementById("config-json").value);
          renderConfigControls();
          document.getElementById("config-status").textContent = `${configData.length || 0} engines`;
          document.getElementById("config-status").className = "card-sub";
        } catch (err) {
          document.getElementById("config-status").textContent = err.message;
          document.getElementById("config-status").className = "card-sub err";
          document.getElementById("raw-config-toggle").checked = true;
          return;
        }
      }
      rawConfigMode = enabled;
      syncConfigMode();
    }

    async function saveConfig() {
      const status = document.getElementById("config-status");
      status.textContent = "Saving…"; status.className = "card-sub";
      const text = rawConfigMode
        ? document.getElementById("config-json").value
        : JSON.stringify(configData, null, 4);
      const resp = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text })
      });
      const data = await resp.json();
      if (!resp.ok || data.error) {
        status.textContent = data.error || "Save failed";
        status.className = "card-sub err";
        return;
      }
      status.textContent = data.backup ? `Saved · backup ${data.backup}` : "Saved";
      status.className = "card-sub ok-txt";
      await loadConfig();
      await refresh();
    }

    document.getElementById("theme-toggle").addEventListener("click", () => applyTheme(activeTheme === "dark" ? "light" : "dark"));
    document.getElementById("board-view-tab").addEventListener("click", () => setActiveView("board"));
    document.getElementById("learner-view-tab").addEventListener("click", () => setActiveView("learner"));
    document.getElementById("follow-toggle").addEventListener("change", e => setFollowLive(e.target.checked));
    document.getElementById("prev-move").addEventListener("click", () => navigateMove(-1));
    document.getElementById("next-move").addEventListener("click", () => navigateMove(1));
    document.getElementById("flip-board").addEventListener("click", () => setBoardOrientation(boardOrientation === "white" ? "black" : "white"));
    document.getElementById("analysis-toggle").addEventListener("change", e => setAnalysisEnabled(e.target.checked));
    document.getElementById("analysis-panel-toggle").addEventListener("change", e => setAnalysisEnabled(e.target.checked));
    document.getElementById("matches-prev").addEventListener("click", () => setPreviousMatchesPage(-1));
    document.getElementById("matches-next").addEventListener("click", () => setPreviousMatchesPage(1));
    document.getElementById("copy-pgn-path").addEventListener("click", copyActiveMatchUrl);
    document.getElementById("matches").addEventListener("click", event => {
      const copyButton = event.target.closest("[data-match-copy-index]");
      if (copyButton) {
        event.preventDefault();
        event.stopPropagation();
        copyMatchRowUrl(copyButton.dataset.matchCopyIndex, copyButton);
        return;
      }
      const row = event.target.closest("[data-match-index]");
      if (row) loadPreviousMatch(row.dataset.matchIndex);
    });
    document.querySelectorAll("[data-log-side]").forEach(button => {
      button.addEventListener("click", () => setLogSideFilter(button.dataset.logSide));
    });
    document.querySelectorAll("[data-log-kind]").forEach(button => {
      button.addEventListener("click", () => setLogKindFilter(button.dataset.logKind));
    });
    window.addEventListener("keydown", e => {
      const tag = document.activeElement ? document.activeElement.tagName : "";
      if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
      if (e.key === "ArrowLeft") { e.preventDefault(); navigateMove(-1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); navigateMove(1); }
    });
    document.getElementById("raw-config-toggle").addEventListener("change", e => setRawConfigMode(e.target.checked));
    document.getElementById("config-controls").addEventListener("input", e => updateConfigFromInput(e.target));
    document.getElementById("config-controls").addEventListener("change", e => updateConfigFromInput(e.target));
    document.getElementById("config-save").addEventListener("click", saveConfig);
    document.getElementById("engine-config-card").addEventListener("toggle", e => setEngineConfigExpanded(e.target.open));
    window.addEventListener("hashchange", () => {
      if (suppressHashChange) return;
      if (selectPreviousMatchFromHash()) {
        renderPreviousMatches();
        refresh(true);
      }
    });

    loadPreferences();
    setActiveView(localStorage.getItem("livePgnView") || "board");
    refresh();
    loadStats();
    loadConfig();
    loadLearner();
    setInterval(refresh, 1000);
    setInterval(updateClockDisplays, 250);
    setInterval(loadStats, 3000);
    setInterval(loadLearner, 2500);
    checkViewerVersion();
    setInterval(checkViewerVersion, 1200);
  </script>
</body>
</html>
"""


def load_stockfish_path(config_path: Path) -> Path:
    engines = json.loads(config_path.read_text(encoding="utf-8"))
    for engine in engines:
        name = engine.get("name", "")
        if "stockfish" in name.lower() and engine.get("enabled", True):
            path = Path(engine.get("path", ""))
            if path.exists():
                return path
    raise RuntimeError(f"No enabled Stockfish executable found in {config_path}")


def engine_display_name(path: Path) -> str:
    name = path.stem
    return name.replace("stockfish-", "Stockfish ").replace("_", " ")


class StockfishAnalyzer:
    def __init__(self, config_path: Path, movetime_ms: int, multipv: int, enabled: bool = True):
        self.config_path = config_path
        self.movetime_ms = movetime_ms
        self.multipv = max(1, multipv)
        self.enabled = enabled
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.exe: Path | None = None
        self.cache: dict[str, dict] = {}

    def reset(self) -> None:
        with self.lock:
            self.cache.clear()
            self._close_locked()
            self.exe = None

    def analyze(self, board: chess.Board) -> dict:
        if not self.enabled:
            return {"enabled": False}
        if board.is_game_over(claim_draw=True):
            return {"enabled": True, "message": "Game over."}

        key = f"{board.fen()}|{self.movetime_ms}|{self.multipv}"
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            try:
                self._ensure_started_locked()
                assert self.exe is not None
                result = self._analyze_locked(board)
            except Exception as exc:
                result = {"enabled": True, "error": str(exc)}
                self._close_locked()
            self.cache[key] = result
            if len(self.cache) > 64:
                self.cache.pop(next(iter(self.cache)))
            return result

    def _ensure_started_locked(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        self.exe = load_stockfish_path(self.config_path)
        self.proc = subprocess.Popen(
            [str(self.exe)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._command_locked("uci")
        self._read_until_locked("uciok")
        self._command_locked("setoption name Threads value 1")
        self._command_locked("setoption name Hash value 32")
        self._command_locked(f"setoption name MultiPV value {self.multipv}")
        self._command_locked("isready")
        self._read_until_locked("readyok")

    def _command_locked(self, line: str) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("Stockfish stdin is closed")
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def _read_until_locked(self, marker: str) -> list[str]:
        if self.proc is None or self.proc.stdout is None:
            raise RuntimeError("Stockfish stdout is closed")
        lines = []
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                raise RuntimeError("Stockfish exited unexpectedly")
            line = line.rstrip("\n")
            lines.append(line)
            if marker in line:
                return lines

    def _analyze_locked(self, board: chess.Board) -> dict:
        assert self.exe is not None
        self._command_locked(f"position fen {board.fen()}")
        self._command_locked(f"go movetime {self.movetime_ms}")
        output = self._read_until_locked("bestmove")
        bestmove = ""
        if output and output[-1].startswith("bestmove "):
            parts = output[-1].split()
            if len(parts) > 1:
                bestmove = parts[1]
        lines = parse_stockfish_info(output, board, bestmove)
        return {
            "enabled": True,
            "engine": engine_display_name(self.exe),
            "path": str(self.exe),
            "movetime_ms": self.movetime_ms,
            "multipv": self.multipv,
            "lines": lines,
        }

    def _close_locked(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                self._command_locked("quit")
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()
        self.proc = None

    def close(self) -> None:
        with self.lock:
            self._close_locked()


def parse_stockfish_info(lines: list[str], board: chess.Board, bestmove: str) -> list[dict]:
    latest: dict[int, dict] = {}
    for line in lines:
        if not line.startswith("info "):
            continue
        tokens = line.split()
        if "score" not in tokens or "pv" not in tokens:
            continue
        multipv = 1
        if "multipv" in tokens:
            multipv_index = tokens.index("multipv")
            if multipv_index + 1 < len(tokens):
                multipv = int(tokens[multipv_index + 1])
        score_index = tokens.index("score")
        pv_index = tokens.index("pv")
        score_type = tokens[score_index + 1] if score_index + 1 < len(tokens) else ""
        score_value = int(tokens[score_index + 2]) if score_index + 2 < len(tokens) else 0
        depth = None
        if "depth" in tokens:
            depth_index = tokens.index("depth")
            if depth_index + 1 < len(tokens):
                depth = int(tokens[depth_index + 1])
        pv = tokens[pv_index + 1 :]
        latest[multipv] = {
            "multipv": multipv,
            "depth": depth,
            "score": format_score(score_type, score_value, board.turn),
            "score_type": score_type,
            "score_value": score_value,
            "pv_uci": " ".join(pv[:8]),
            "pv_san": pv_to_san(board, pv[:8]),
            "bestmove": bestmove,
        }
    return [latest[index] for index in sorted(latest)]


def format_score(score_type: str, value: int, turn: bool) -> str:
    sign = 1 if turn == chess.WHITE else -1
    white_value = value * sign
    if score_type == "mate":
        prefix = "+" if white_value > 0 else "-"
        return f"{prefix}M{abs(white_value)}"
    pawns = white_value / 100
    return f"{pawns:+.2f}"


def pv_to_san(board: chess.Board, pv: list[str]) -> str:
    temp = board.copy(stack=False)
    san_moves = []
    for uci in pv:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            break
        if move not in temp.legal_moves:
            break
        san_moves.append(temp.san(move))
        temp.push(move)
    return " ".join(san_moves)


def clock_from_headers(headers: chess.pgn.Headers, completed: bool) -> dict | None:
    try:
        white_ms = int(headers.get("WhiteClockMs", ""))
        black_ms = int(headers.get("BlackClockMs", ""))
        updated_at = int(headers.get("ClockUpdatedAtEpochMs", ""))
    except ValueError:
        return None
    running_side = headers.get("ClockRunningSide", "")
    if running_side not in {"White", "Black"}:
        running_side = ""
    return {
        "white_ms": white_ms,
        "black_ms": black_ms,
        "updated_at_epoch_ms": updated_at,
        "server_now_epoch_ms": int(time.time() * 1000),
        "running_side": running_side,
        "completed": completed,
    }


def parse_clk_comment(comment: str) -> int | None:
    match = CLK_COMMENT_RE.search(comment or "")
    if not match:
        return None
    parts = match.group("value").split(":")
    try:
        seconds = float(parts[-1])
        minutes = int(parts[-2])
        hours = int(parts[-3]) if len(parts) == 3 else 0
    except (ValueError, IndexError):
        return None
    return int(round(((hours * 60 + minutes) * 60 + seconds) * 1000))


def clock_from_comments(game: chess.pgn.Game, completed: bool) -> dict | None:
    white_ms: int | None = None
    black_ms: int | None = None
    ply = 0
    for node in game.mainline():
        ply += 1
        clock_ms = parse_clk_comment(node.comment)
        if clock_ms is None:
            continue
        if ply % 2:
            white_ms = clock_ms
        else:
            black_ms = clock_ms
    if white_ms is None and black_ms is None:
        return None
    running_side = ""
    if not completed:
        running_side = "White" if ply % 2 == 0 else "Black"
    now_ms = int(time.time() * 1000)
    return {
        "white_ms": white_ms,
        "black_ms": black_ms,
        "updated_at_epoch_ms": now_ms,
        "server_now_epoch_ms": now_ms,
        "running_side": running_side,
        "completed": completed,
    }


def clock_from_game(game: chess.pgn.Game, completed: bool) -> dict | None:
    return clock_from_headers(game.headers, completed) or clock_from_comments(game, completed)


def tournament_slug(path: Path) -> str:
    slug = path.stem
    if slug.endswith("-live"):
        slug = slug.removesuffix("-live")
    slug = re.sub(r"(-live-\d{8}-\d{6})-game-\d+$", r"\1", slug)
    return slug or path.name


def live_slug_epoch(slug: str) -> float | None:
    match = re.search(r"live-(\d{8})-(\d{6})", slug or "")
    if not match:
        return None
    try:
        return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        return None


def read_game(
    path: Path,
    analyzer: StockfishAnalyzer | None = None,
    include_analysis: bool = True,
    analysis_ply: int | None = None,
    game_index: int | None = None,
    include_logs: bool = False,
) -> dict:
    if not path.exists():
        result = {"exists": False, "has_game": False, "path": str(path), "tournament_slug": tournament_slug(path)}
        if analyzer is not None and include_analysis:
            result["analysis"] = {"enabled": analyzer.enabled, "message": "No board to analyze."}
        return result

    try:
        text = path.read_text(encoding="utf-8")
        stream = io.StringIO(text)
        games = []
        while True:
            parsed_game = chess.pgn.read_game(stream)
            if parsed_game is None:
                break
            games.append(parsed_game)
        game_count = len(games)
        selected_index = game_count if game_index is None else max(1, min(game_index, game_count))
        game = games[selected_index - 1] if games else None
    except Exception as exc:
        return {"exists": True, "has_game": False, "path": str(path), "tournament_slug": tournament_slug(path), "error": str(exc)}

    if game is None:
        stat = path.stat()
        result = {
            "exists": True,
            "has_game": False,
            "path": str(path),
            "tournament_slug": tournament_slug(path),
            "mtime": stat.st_mtime,
            "size": stat.st_size,
        }
        if analyzer is not None and include_analysis:
            result["analysis"] = {"enabled": analyzer.enabled, "message": "No board to analyze."}
        return result

    board = game.board()
    moves = []
    positions = [{"ply": 0, "fen": board.fen(), "turn": "White", "last_move": None}]
    last_move = None
    for ply, move in enumerate(game.mainline_moves(), start=1):
        san = board.san(move)
        from_square = chess.square_name(move.from_square)
        to_square = chess.square_name(move.to_square)
        board.push(move)
        moves.append(
            {
                "ply": ply,
                "move_number": (ply + 1) // 2,
                "side": "White" if ply % 2 else "Black",
                "san": san,
                "uci": move.uci(),
            }
        )
        last_move = {"from": from_square, "to": to_square, "uci": move.uci()}
        positions.append(
            {
                "ply": ply,
                "fen": board.fen(),
                "turn": "White" if board.turn == chess.WHITE else "Black",
                "last_move": last_move,
                "san": san,
            }
        )

    stat = path.stat()
    result_header = game.headers.get("Result", "*")
    completed = result_header != "*"
    try:
        total_games = int(game.headers.get("TotalGames", "0") or 0)
    except ValueError:
        total_games = 0
    result = {
        "exists": True,
        "has_game": True,
        "path": str(path),
        "tournament_slug": tournament_slug(path),
        "mtime": stat.st_mtime,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        "size": stat.st_size,
        "game_count": game_count,
        "game_index": selected_index,
        "total_games": total_games,
        "headers": dict(game.headers),
        "fen": board.fen(),
        "turn": "White" if board.turn == chess.WHITE else "Black",
        "completed": completed,
        "last_move": last_move,
        "moves": moves,
        "positions": positions,
    }
    clock = clock_from_game(game, completed)
    if clock is not None:
        result["clock"] = clock
    if include_logs:
        result["logs"] = collect_game_logs(game.headers, positions, moves)
    if analyzer is not None and include_analysis:
        if analysis_ply is None:
            analysis_board = board
            result["analysis_ply"] = len(moves)
        else:
            clamped_ply = max(0, min(analysis_ply, len(positions) - 1))
            analysis_board = chess.Board(positions[clamped_ply]["fen"])
            result["analysis_ply"] = clamped_ply
        result["analysis"] = analyzer.analyze(analysis_board)
    return result


def parse_filter_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_pgn_date(value: str | None, fallback_mtime: float) -> date:
    if value:
        normalized = value.replace(".", "-")
        if "?" not in normalized:
            try:
                return datetime.strptime(normalized, "%Y-%m-%d").date()
            except ValueError:
                pass
    return datetime.fromtimestamp(fallback_mtime).date()


def result_points(result: str, side: str) -> tuple[float, str] | None:
    if result == "1/2-1/2":
        return 0.5, "draw"
    if result == "1-0":
        return (1.0, "win") if side == "white" else (0.0, "loss")
    if result == "0-1":
        return (1.0, "win") if side == "black" else (0.0, "loss")
    return None


def result_label(result: str, white: str, black: str) -> str:
    if result == "1-0":
        return f"{white} (White) won"
    if result == "0-1":
        return f"{black} (Black) won"
    if result == "1/2-1/2":
        return "Draw"
    return result


def live_status_path_for(pgn_path: Path) -> Path:
    return pgn_path.with_suffix(".status.json")


def live_pgn_path_for_status(status_path: Path) -> Path:
    if status_path.name.endswith(".status.json"):
        return status_path.with_name(status_path.name[: -len(".status.json")] + ".pgn")
    return status_path.with_suffix(".pgn")


def live_selection_path_for(pgn_path: Path) -> Path:
    return pgn_path.with_suffix(".selection.json")


def write_live_selection(pgn_path: Path, game_index: int) -> dict:
    if game_index < 1:
        raise ValueError("Live game index must be at least 1.")
    path = live_selection_path_for(pgn_path)
    payload = {
        "locked_game": game_index,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at_epoch": time.time(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(path), "locked_game": game_index}


def live_pgn_has_in_progress_clock(pgn_path: Path) -> bool:
    try:
        with pgn_path.open("r", encoding="utf-8", errors="replace") as handle:
            game = chess.pgn.read_game(handle)
    except Exception:
        return False
    if game is None:
        return False
    if game.headers.get("Result", "*") != "*":
        return False
    running_side = game.headers.get("ClockRunningSide", "")
    if running_side not in {"White", "Black"}:
        return True
    clock_key = "WhiteClockMs" if running_side == "White" else "BlackClockMs"
    try:
        remaining_ms = int(game.headers.get(clock_key, "0"))
        updated_at_ms = int(game.headers.get("ClockUpdatedAtEpochMs", "0"))
    except ValueError:
        return True
    return updated_at_ms <= 0 or updated_at_ms + remaining_ms > int(time.time() * 1000)


def live_status_matches(out_dir: Path, status_path: Path, fallback_pgn_path: Path | None = None) -> list[dict]:
    try:
        mtime = status_path.stat().st_mtime
        if time.time() - mtime > LIVE_STATUS_MAX_AGE_SECONDS:
            return []
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    output_value = payload.get("output_pgn")
    output_pgn = Path(output_value) if output_value else fallback_pgn_path
    control_value = payload.get("control_pgn")
    control_pgn = Path(control_value) if control_value else (fallback_pgn_path or output_pgn)
    if output_pgn is None:
        return []
    if not output_pgn.is_absolute():
        output_pgn = fallback_pgn_path if fallback_pgn_path is not None else live_pgn_path_for_status(status_path)
    if control_pgn is not None and not control_pgn.is_absolute():
        control_pgn = fallback_pgn_path if fallback_pgn_path is not None else live_pgn_path_for_status(status_path)
    if not output_pgn.exists():
        return []
    if not live_pgn_has_in_progress_clock(output_pgn):
        return []

    slug = tournament_slug(output_pgn)
    started_epoch = live_slug_epoch(slug)
    updated_at = payload.get("generated_at") or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
    updated_epoch = float(payload.get("generated_at_epoch") or mtime)
    locked_game = payload.get("locked_game")
    matches: list[dict] = []
    for game in payload.get("games", []):
        if game.get("finished"):
            continue
        game_no = int(game.get("game") or 0)
        is_board_game = locked_game is not None and game_no == int(locked_game)
        matches.append(
            {
                "kind": "live",
                "date": "In progress",
                "updated_at": updated_at,
                "updated_at_epoch": updated_epoch,
                "live_started_epoch": started_epoch or updated_epoch,
                "white": game.get("white") or "White",
                "black": game.get("black") or "Black",
                "result": "*",
                "winner_label": "In progress",
                "status_label": "In progress",
                "round": str(game_no),
                "tournament_slug": slug,
                "file": str(output_pgn.relative_to(out_dir)) if output_pgn.is_relative_to(out_dir) else str(output_pgn),
                "path": str(output_pgn.resolve()),
                "control_path": str((control_pgn or output_pgn).resolve()),
                "game_index": game_no,
                "total": int(game.get("total") or 0),
                "is_board_game": is_board_game,
            }
        )
    return matches


def collect_live_matches(out_dir: Path, live_pgn_path: Path | None) -> list[dict]:
    status_paths: list[Path] = []
    if live_pgn_path is not None:
        status_paths.append(live_status_path_for(live_pgn_path))
    live_dir = out_dir / "live"
    if live_dir.exists():
        status_paths.extend(sorted(live_dir.glob("*.status.json"), key=lambda path: path.stat().st_mtime, reverse=True))

    matches: list[dict] = []
    seen_statuses: set[Path] = set()
    seen_games: set[tuple[str, int]] = set()
    for status_path in status_paths:
        resolved_status = status_path.resolve()
        if resolved_status in seen_statuses:
            continue
        seen_statuses.add(resolved_status)
        fallback = live_pgn_path if live_pgn_path is not None and live_status_path_for(live_pgn_path).resolve() == resolved_status else live_pgn_path_for_status(status_path)
        for match in live_status_matches(out_dir, status_path, fallback):
            key = (match.get("path") or "", int(match.get("game_index") or 1))
            if key in seen_games:
                continue
            seen_games.add(key)
            matches.append(match)
    return matches


def collect_stats(out_dir: Path, date_from: date | None, date_to: date | None, live_pgn_path: Path | None = None) -> dict:
    stats: dict[str, dict] = {}
    matches: list[dict] = collect_live_matches(out_dir, live_pgn_path)
    completed_games = 0
    excluded_archive_dirs = {"live", "backups"}
    pgn_files = [
        path
        for path in sorted(out_dir.rglob("*.pgn"))
        if not (excluded_archive_dirs & {part.lower() for part in path.relative_to(out_dir).parts})
    ]

    for pgn_path in pgn_files:
        try:
            mtime = pgn_path.stat().st_mtime
            with pgn_path.open("r", encoding="utf-8", errors="replace") as handle:
                game_index = 0
                while True:
                    game = chess.pgn.read_game(handle)
                    if game is None:
                        break
                    game_index += 1
                    result = game.headers.get("Result", "*")
                    if result not in {"1-0", "0-1", "1/2-1/2"}:
                        continue
                    game_date = parse_pgn_date(game.headers.get("Date"), mtime)
                    if date_from and game_date < date_from:
                        continue
                    if date_to and game_date > date_to:
                        continue
                    white = game.headers.get("White", "White")
                    black = game.headers.get("Black", "Black")
                    round_name = game.headers.get("Round", "")
                    completed_games += 1
                    matches.append(
                        {
                            "date": game_date.isoformat(),
                            "kind": "completed",
                            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                            "updated_at_epoch": mtime,
                            "white": white,
                            "black": black,
                            "result": result,
                            "winner_label": result_label(result, white, black),
                            "status_label": result,
                            "round": round_name,
                            "tournament_slug": tournament_slug(pgn_path),
                            "file": str(pgn_path.relative_to(out_dir)),
                            "path": str(pgn_path.resolve()),
                            "game_index": game_index,
                            "is_board_game": False,
                        }
                    )
                    for side, engine in (("white", white), ("black", black)):
                        entry = stats.setdefault(
                            engine,
                            {"engine": engine, "games": 0, "wins": 0, "draws": 0, "losses": 0, "points": 0.0},
                        )
                        points = result_points(result, side)
                        if points is None:
                            continue
                        score, outcome = points
                        entry["games"] += 1
                        entry["points"] += score
                        if outcome == "win":
                            entry["wins"] += 1
                        elif outcome == "draw":
                            entry["draws"] += 1
                        else:
                            entry["losses"] += 1
        except Exception:
            continue

    engines = sorted(stats.values(), key=lambda item: (-item["points"], -item["wins"], item["engine"].lower()))
    matches.sort(
        key=lambda item: (
            item.get("live_started_epoch", item["updated_at_epoch"]),
            item["updated_at_epoch"],
            item["date"],
            item["file"],
            item["round"],
        ),
        reverse=True,
    )
    for match in matches:
        match.pop("updated_at_epoch", None)
        match.pop("live_started_epoch", None)
    for entry in engines:
        entry["points"] = int(entry["points"]) if entry["points"].is_integer() else entry["points"]
    return {
        "games": completed_games,
        "engines": engines,
        "matches": matches,
        "filters": {
            "from": date_from.isoformat() if date_from else None,
            "to": date_to.isoformat() if date_to else None,
        },
    }


def read_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {"exists": False, "path": str(config_path), "text": "", "engines": 0}
    text = config_path.read_text(encoding="utf-8")
    parsed = json.loads(text)
    return {
        "exists": True,
        "path": str(config_path),
        "text": json.dumps(parsed, indent=4),
        "engines": len(parsed) if isinstance(parsed, list) else 0,
    }


def write_config(config_path: Path, text: str) -> dict:
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError("Engine config must be a JSON array.")
    backup_name = None
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        backup = config_path.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
        backup_name = backup.name
    config_path.write_text(json.dumps(parsed, indent=4), encoding="utf-8")
    return {"ok": True, "path": str(config_path), "backup": backup_name, "engines": len(parsed)}


def size_label(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def read_text_file(path: Path, root: Path | None = None, max_chars: int = 24000) -> dict:
    exists = path.exists()
    item = {
        "name": str(path.relative_to(root)) if root and exists else path.name,
        "path": str(path),
        "exists": exists,
        "text": "",
        "size": 0,
        "size_label": "0 B",
        "updated_at": "",
    }
    if not exists:
        return item
    stat = path.stat()
    text = path.read_text(encoding="utf-8", errors="replace")
    item.update(
        {
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
            "size": stat.st_size,
            "size_label": size_label(stat.st_size),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        }
    )
    return item


def collect_text_files(root: Path, max_files: int = 40) -> list[dict]:
    if not root.exists():
        return []
    allowed = {".md", ".txt", ".json", ".yaml", ".yml"}
    paths = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in allowed and path.stat().st_size <= 512 * 1024
    ]
    paths.sort(key=lambda path: (path.stat().st_mtime, str(path).lower()), reverse=True)
    return [read_text_file(path, root, max_chars=18000) for path in paths[:max_files]]


def classify_log_line(line: str) -> str:
    lower = line.lower()
    if "thread started" in lower:
        return "setup"
    if "decision prompt" in lower:
        return "prompt"
    if "decision comment" in lower:
        return "comment"
    if (
        "illegal codex move" in lower
        or "invalid codex response" in lower
        or "invalid model" in lower
        or "codex turn error" in lower
        or "codex app-server turn failed" in lower
        or "usagelimitexceeded" in lower
    ):
        return "repair"
    if "bestmove" in lower:
        return "move"
    if "account:" in lower:
        return "account"
    return "log"


def parse_log_timestamp(line: str) -> str:
    match = re.match(r"\[(?P<ts>[^\]]+)\]\s*(?P<text>.*)", line)
    if not match:
        return "", line
    return match.group("ts"), match.group("text")


def parse_log_datetime(timestamp: str) -> datetime | None:
    if not timestamp:
        return None
    try:
        return datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def fen_key(fen: str) -> str:
    return " ".join(str(fen or "").split()[:4])


def line_fen_key(line: str) -> str:
    match = re.search(r"\bfen=(.*?)\s+(?:legal_moves=|moves=|go=)", line, flags=re.I)
    if not match:
        match = re.search(r"\bfrom fen=(.*?)\s+go=", line, flags=re.I)
    return fen_key(match.group(1)) if match else ""


def line_move(line: str) -> str:
    match = re.search(r"\b(?:bestmove|move)=([a-h][1-8][a-h][1-8][qrbn]?|0000)\b", line, flags=re.I)
    return match.group(1).lower() if match else ""


def parse_game_log_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S %z")
        return parsed.astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def game_log_window(headers: chess.pgn.Headers) -> tuple[datetime | None, datetime | None]:
    start = parse_game_log_time(headers.get("GameStartTime"))
    end = parse_game_log_time(headers.get("GameEndTime"))
    if start is None and end is None:
        return None, None
    padding = 90
    return (
        datetime.fromtimestamp(start.timestamp() - padding) if start else None,
        datetime.fromtimestamp(end.timestamp() + padding) if end else None,
    )


def bot_from_thread_line(line: str) -> tuple[str, str]:
    lower = line.lower()
    if str(LEARNER_DIR).lower() in lower:
        return "learner", "Codex-chess-learner"
    if "context=" in lower:
        return "baseline", "Codex-chess"
    return "unknown", "Unknown"


def side_from_line(line: str, current_side: str = "") -> str:
    match = re.search(r"\bside=(white|black)\b", line, flags=re.I)
    if match:
        return match.group(1).lower()
    match = re.search(r"\bside_to_move[\"']?\s*[:=]\s*[\"']?(white|black)\b", line, flags=re.I)
    if match:
        return match.group(1).lower()
    return current_side


def collect_learner_logs(
    max_entries: int = 80,
    max_files: int = 30,
    per_file_tail: int | None = 14,
    target_fens: set[tuple[str, str]] | None = None,
    target_moves: set[str] | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> tuple[list[dict], int]:
    if not ENGINE_LOG_DIR.exists():
        return [], 0
    entries: list[dict] = []
    learner_log_count = 0
    paths = sorted(ENGINE_LOG_DIR.glob("codex-chess-*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in paths[:max_files]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        current_bot = "unknown"
        current_bot_label = "Unknown"
        current_side = ""
        interesting: list[tuple[str, str, str, str]] = []
        for line in lines:
            if "thread started:" in line:
                current_bot, current_bot_label = bot_from_thread_line(line)
                if current_bot == "learner":
                    learner_log_count += 1
            current_side = side_from_line(line, current_side)
            if any(
                marker in line
                for marker in (
                    "thread started:",
                    "decision prompt:",
                    "decision comment:",
                    "illegal Codex move",
                    "invalid Codex response",
                    "invalid model",
                    "codex turn error",
                    "Codex app-server turn failed",
                    "bestmove ",
                )
            ):
                interesting.append((line, current_bot, current_bot_label, current_side))
        selected_lines = interesting[-per_file_tail:] if per_file_tail is not None else interesting
        for line, bot, bot_label, side in selected_lines:
            timestamp, text = parse_log_timestamp(line)
            parsed_time = parse_log_datetime(timestamp)
            if window_start and (parsed_time is None or parsed_time < window_start):
                continue
            if window_end and (parsed_time is None or parsed_time > window_end):
                continue
            entry_fen = line_fen_key(text)
            entry_move = line_move(text)
            if target_fens is not None or target_moves is not None:
                side_key = (side or "").lower()
                fen_match = bool(entry_fen and target_fens and ((entry_fen, side_key) in target_fens or (entry_fen, "") in target_fens))
                move_match = bool(entry_move and target_moves and entry_move in target_moves)
                if not fen_match and not move_match:
                    continue
            entries.append(
                {
                    "timestamp": timestamp,
                    "text": text,
                    "kind": classify_log_line(text),
                    "bot": bot,
                    "bot_label": bot_label,
                    "side": side,
                    "file": path.name,
                    "fen_key": entry_fen,
                    "uci": entry_move,
                }
            )
    entries.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
    return entries[:max_entries], learner_log_count


def collect_game_logs(headers: chess.pgn.Headers, positions: list[dict], moves: list[dict]) -> list[dict]:
    target_fens: set[tuple[str, str]] = set()
    target_moves: set[str] = set()
    positions_by_ply = {int(item.get("ply", 0)): item for item in positions}
    for move in moves:
        ply = int(move.get("ply", 0))
        before = positions_by_ply.get(max(0, ply - 1))
        side = str(move.get("side", "")).lower()
        if before:
            target_fens.add((fen_key(str(before.get("fen", ""))), side))
        if move.get("uci"):
            target_moves.add(str(move["uci"]).lower())
    window_start, window_end = game_log_window(headers)
    logs, _ = collect_learner_logs(
        max_entries=1200,
        max_files=500,
        per_file_tail=None,
        target_fens=target_fens,
        target_moves=target_moves,
        window_start=window_start,
        window_end=window_end,
    )
    return logs


def collect_learner_data() -> dict:
    memory = read_text_file(LEARNER_MEMORY_PATH, LEARNER_DIR)
    knowledgebase = collect_text_files(LEARNER_KNOWLEDGEBASE_DIR)
    skills = collect_text_files(LEARNER_SKILLS_DIR)
    logs, learner_log_count = collect_learner_logs()
    return {
        "root": str(LEARNER_DIR),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "memory": memory,
        "knowledgebase": knowledgebase,
        "skills": skills,
        "logs": logs,
        "summary": {
            "memory": memory["size_label"] if memory["exists"] else "missing",
            "knowledgebase_files": len(knowledgebase),
            "skill_files": len(skills),
            "learner_logs": learner_log_count,
        },
    }


class LivePgnHandler(BaseHTTPRequestHandler):
    pgn_path = DEFAULT_PGN_PATH
    config_path = DEFAULT_ENGINE_CONFIG
    stats_dir = OUT_DIR
    analyzer: StockfishAnalyzer | None = None
    viewer_version = ""
    hot_reload = False

    def log_message(self, format: str, *args) -> None:
        return

    def send_bytes(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_bytes(body, "application/json; charset=utf-8", status)

    def read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return

        if parsed.path == "/api/game":
            query = parse_qs(parsed.query)
            raw_path = query.get("path", [str(self.pgn_path)])[0]
            pgn_path = Path(raw_path)
            if not pgn_path.is_absolute():
                pgn_path = self.stats_dir / pgn_path
            include_analysis = query.get("analysis", ["1"])[0] not in {"0", "false", "off"}
            include_logs = query.get("logs", ["0"])[0] not in {"0", "false", "off"}
            game_index = None
            if query.get("game", [""])[0] != "":
                game_index = int(query.get("game", ["0"])[0])
            analysis_ply = None
            if query.get("ply", [""])[0] != "":
                analysis_ply = int(query.get("ply", ["0"])[0])
            self.send_json(read_game(pgn_path, self.analyzer, include_analysis, analysis_ply, game_index, include_logs))
            return

        if parsed.path == "/api/config":
            try:
                self.send_json(read_config(self.config_path))
            except Exception as exc:
                self.send_json({"exists": self.config_path.exists(), "path": str(self.config_path), "error": str(exc)})
            return

        if parsed.path == "/api/stats":
            query = parse_qs(parsed.query)
            try:
                date_from = parse_filter_date(query.get("from", [None])[0])
                date_to = parse_filter_date(query.get("to", [None])[0])
                live_pgn_path = Path(query.get("live", [str(self.pgn_path)])[0] or str(self.pgn_path))
                if not live_pgn_path.is_absolute():
                    live_pgn_path = self.stats_dir / live_pgn_path
                self.send_json(collect_stats(self.stats_dir, date_from, date_to, live_pgn_path))
            except Exception as exc:
                self.send_json({"games": 0, "engines": [], "error": str(exc)})
            return

        if parsed.path == "/api/learner":
            try:
                self.send_json(collect_learner_data())
            except Exception as exc:
                self.send_json({"root": str(LEARNER_DIR), "error": str(exc)})
            return

        if parsed.path == "/api/viewer-version":
            self.send_json({"version": self.viewer_version, "hot_reload": self.hot_reload})
            return

        self.send_bytes(b"Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            try:
                payload = self.read_json_body()
                result = write_config(self.config_path, str(payload.get("text", "")))
                if self.analyzer is not None:
                    self.analyzer.reset()
                self.send_json(result)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/live-selection":
            try:
                payload = self.read_json_body()
                raw_path = str(payload.get("path") or self.pgn_path)
                pgn_path = Path(raw_path)
                if not pgn_path.is_absolute():
                    pgn_path = self.stats_dir / pgn_path
                result = write_live_selection(pgn_path, int(payload.get("game") or 1))
                self.send_json(result)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_bytes(b"Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)


def hot_reload_stamp() -> str:
    parts: list[str] = []
    for path in HOT_RELOAD_FILES:
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
    return "|".join(parts)


def run_hot_reload_supervisor(argv: list[str]) -> int:
    env = os.environ.copy()
    env[HOT_RELOAD_ENV] = "1"
    command = [sys.executable, *argv]
    stamp = hot_reload_stamp()
    child: subprocess.Popen | None = None
    try:
        while True:
            child = subprocess.Popen(command, cwd=str(ROOT), env=env)
            while True:
                code = child.poll()
                if code is not None:
                    return int(code)
                time.sleep(0.8)
                next_stamp = hot_reload_stamp()
                if next_stamp != stamp:
                    stamp = next_stamp
                    print("Viewer source changed; restarting local viewer.", flush=True)
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                    break
    except KeyboardInterrupt:
        if child and child.poll() is None:
            child.terminate()
        return 130


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pgn", type=Path, default=DEFAULT_PGN_PATH)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", type=Path, default=DEFAULT_ENGINE_CONFIG)
    parser.add_argument("--stats-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--analysis-movetime-ms", type=int, default=250)
    parser.add_argument("--analysis-multipv", type=int, default=3)
    parser.add_argument("--no-analysis", action="store_true")
    parser.add_argument("--hot-reload", action="store_true", help="Restart the viewer server and reload the page when viewer source files change.")
    args = parser.parse_args()

    if args.hot_reload and os.environ.get(HOT_RELOAD_ENV) != "1":
        raise SystemExit(run_hot_reload_supervisor(sys.argv))

    analyzer = StockfishAnalyzer(
        config_path=args.config,
        movetime_ms=args.analysis_movetime_ms,
        multipv=args.analysis_multipv,
        enabled=not args.no_analysis,
    )
    LivePgnHandler.pgn_path = args.pgn
    LivePgnHandler.config_path = args.config
    LivePgnHandler.stats_dir = args.stats_dir
    LivePgnHandler.analyzer = analyzer
    LivePgnHandler.viewer_version = hot_reload_stamp()
    LivePgnHandler.hot_reload = args.hot_reload

    server = ThreadingHTTPServer((args.host, args.port), LivePgnHandler)
    print(f"Live PGN viewer: http://{args.host}:{args.port}/")
    print(f"PGN: {args.pgn}")
    print(f"Engine config: {args.config}")
    print("Stockfish analysis: " + ("off" if args.no_analysis else f"{args.analysis_multipv} PV, {args.analysis_movetime_ms}ms"))
    try:
        server.serve_forever()
    finally:
        analyzer.close()


if __name__ == "__main__":
    main()
