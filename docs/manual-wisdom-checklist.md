# Manual TWIC wisdom — step-by-step checklist

Prompt: [manual-wisdom-prompt.md](manual-wisdom-prompt.md)  
Output: [manual-wisdom-master-games.md](manual-wisdom-master-games.md)  
Bulk helper (optional): `tools/manual_twic_wisdom.py`  
State ledger: `out/twic-manual-wisdom/state.json`

---

## How training actually works (two layers)

| Layer | What it does | What it does **not** do |
|---|---|---|
| **A — Human/LLM study** | Board-verified move-by-move study (`python-chess` + FEN at phase boundaries); ask concession-pressure / conversion-delta questions; write portable principles | Memorize opening lines or FEN→move rules; infer structure from PGN text alone |
| **B — Bulk parser** (`manual_twic_wisdom.py`) | Download TWIC zips; count corpus signals (castling, checks, phase); track coverage | Replace Layer A; does not "learn" each game |

**Current repo state (2026-06-04):** Layer B ran ~75 issues (TWIC 1647→1573, ~537k games). Layer A completed Step 1 (10 games from TWIC 1647) in `manual-wisdom-master-games.md`.

---

## Checklist

### Phase 0 — Align on the method
- [x] **0.1** Goal: **wisdom doc only** (`manual-wisdom-master-games.md`)
- [x] **0.2** Output file: `manual-wisdom-master-games.md`
- [x] **0.3** Game filter: **decisive + both players ≥ 2500 Elo** (when listed)
- [x] **0.4** Stop rule: **fixed date 2025-01-01**
- [x] **0.5** Pace: **10 games per step**, then user review

### Phase 1 — Pilot one TWIC issue (Layer A)
- [x] **1.1** Pick one issue: **TWIC 1647** / `twic1647g.zip`
- [x] **1.2** Download & unzip (cached)
- [x] **1.3** Select 10 decisive games (both ≥ 2500 Elo)
- [x] **1.4** Study questions answered for all 10
- [x] **1.5** Wrote step 1 entries to `manual-wisdom-master-games.md`
- [x] **1.6** User review — continuing with step 2 (more from 1647)

## Current step

**Batch mode** — `python tools/run_twic_wisdom_batch.py` processes all decisive games per TWIC (1647→1573), appends to [manual-wisdom-twic-ledger.md](manual-wisdom-twic-ledger.md), commits/pushes after each issue.

Manual step-by-step (Steps 1–2) remains in [manual-wisdom-master-games.md](manual-wisdom-master-games.md) as reference samples.
- [x] **2.1** Continue TWIC **1647** (step 2 — next 10 games)
- [x] **2.2** Hypothesis stated from prior principles
- [x] **2.3** Studied 10 games; noted new vs duplicate
- [x] **2.4** Appended step 2 + updated coverage ledger
- [ ] **2.5** Stop when stop rule triggers OR you say "plateau"

### Phase 3 — Corpus cross-check (Layer B, optional)
- [ ] **3.1** Run or refresh `manual_twic_wisdom.py` for coverage stats
- [ ] **3.2** Compare bulk signals to Layer A principles (sanity check, not source of truth)
- [ ] **3.3** Render doc via `tools/render_manual_wisdom_doc.py` if using bulk ledger

### Phase 4 — Engine use (only if you chose 0.1 = engine)
- [ ] **4.1** Map principles → eval/search heuristics in target engine
- [ ] **4.2** Run depth gate / test games
- [ ] **4.3** Log wins/losses back into wisdom doc

### Phase 5 — Done criteria
- [ ] **5.1** Coverage ledger shows target date/range complete
- [ ] **5.2** Last K issues added zero promoted principles (plateau)
- [ ] **5.3** Top principles list is stable and portable (no move recipes)

---

## Board-verified workflow (Layer A — required)

PGN text is not enough. Step the board before writing structural claims.

1. **Load game** — `python tools/twic_game_board.py <pgn> --phases` or `--plies 34,35,36`
2. **Push moves on the board** — python-chess; use FEN at phase boundaries (opening / lever / tactics / conversion / endgame)
3. **Read structure from FEN** — pawns on each file, king squares, piece scope; then ask the study questions
4. **Write notes** — concession-pressure and conversion-delta chains must match the verified board
5. **Append entry** — use template below; include FEN only at critical plies when it helps

Helper: `tools/twic_game_board.py`  
Example: `python tools/twic_game_board.py out/twic-manual-wisdom/step3-top-game.pgn --plies 34,35,36,40`

---

## Study questions (ask for every decisive game)

1. **Concession pressure:** What did the loser lose by playing normal moves?
2. **Conversion-delta:** How did each winner move make the advantage safer or more permanent?
3. **Counterplay:** What opponent checks/captures/breaks were denied before material was taken?
4. **Horizon:** Was there a quiet setup move depth-limited engines might undervalue?
5. **Portable lesson:** One sentence a human could apply without memorizing this line?

**Skip:** draws, games where our side lost (unless studying opponent's winning science abstractly).

---

## Entry template (append to wisdom doc)

```markdown
## YYYY-MM-DD — TWIC NNNN — [Event or game label]

**Game:** White vs Black, Result, Event, Date  
**Hypothesis tested:** …  
**Board (critical ply):** `FEN …` (optional — from twic_game_board.py)  
**Concession pressure chain:** …  
**Conversion-delta chain:** …  
**Lesson (general):** …  
**New vs prior:** new | reinforces | contradicts  
**Next:** one focused follow-up
```

---

## Current step

**Waiting on your review** — see choices at bottom of `manual-wisdom-master-games.md`.

### Phase 2 — Repeat per issue (newest → older)
