# chess-wisdom — winning principles from decisive master games

Updated: 2026-06-05T15:52:50Z

Latest source: **twic1647g.zip** · [manual-wisdom-sources.md](manual-wisdom-sources.md)

Source prompt: [manual-wisdom-prompt.md](manual-wisdom-prompt.md)  
Checklist: [manual-wisdom-checklist.md](manual-wisdom-checklist.md)

This document holds **portable wisdom only** — principles and corpus patterns that influence wins. It does not list individual games, players, or move sequences.

---

## Method

| Layer | Role |
|---|---|
| **A — Board-verified study** | python-chess + FEN at phase boundaries; concession-pressure and conversion-delta questions |
| **B — Corpus heuristics** | `tools/run_twic_wisdom_batch.py` tags themes across all decisive games; validates patterns, not move recipes |

Layer A rule: step the board before structural claims — PGN text alone is not enough.

---

## Coverage

| Field | Value |
|---|---|
| TWIC issues | **99** (1647 → 1549) |
| Latest archive | **twic1647g.zip** |
| Decisive games analyzed | **623,110** |
| Game date span | 2023-04-17 → 2026-06-01 |
| Source list | [manual-wisdom-sources.md](manual-wisdom-sources.md) |
| Target cutoff | 2025-01-01 |
| Batch runner | `python tools/run_twic_wisdom_batch.py` |
| Re-render wisdom | `python tools/synthesize_twic_wisdom.py` |

---

## Sources

Latest archive: **twic1647g.zip** · [The Week in Chess](https://theweekinchess.com/twic)

**99 TWIC issues processed** (1647 → 1549) — **623,110** decisive games · dates 2023-04-17 → 2026-06-01.

Full per-issue list: [manual-wisdom-sources.md](manual-wisdom-sources.md) · [manual-wisdom-sources.json](manual-wisdom-sources.json)

---

## Formula

**Win = concession pressure × conversion delta × counterplay denial**

- **Concession pressure:** normal replies cost shelter, tempo, coordination, or pawn structure.
- **Conversion delta:** each transformation makes the advantage safer and more permanent.
- **Counterplay denial:** checks, breaks, and trades are answered before material is collected.

---

## Principles (promoted)

Rank is historical; **weight** is what matters at the board. See [the stack](#remember-the-weights-the-stack).

| # | Weight | Principle | Command |
|---|---|---|---|
| 1 | Bedrock ●●● | Normal moves become concessions | What does the opponent lose by defending normally? |
| 2 | Beam ●●○ | One weakness is not enough | Fix one, then create or switch to a second target |
| 3 | Bedrock ●●● | Deny counterplay before collecting | List checks, breaks, trades before grabbing material |
| 4 | Timber ●○○ | Conversion = lower risk, not just more material | Trade when opponent counterplay dies faster than yours grows |
| 5 | Beam ●●○ | Restriction before tactics | Narrow piece scope; the tactic is the receipt, not the plan |
| 6 | Timber ●○○ | Technique is part of the line | After forcing play, name the endgame or mating net |
| 7 | Key 🔑 | Activate the king before collecting | In simplified positions, king moves are part of the winning line |

## Remember the weights (the stack)

Principles are **ordered by weight**, not by rank number. Heavier layers are questions you ask **before** lighter ones — not moves you play instead of them.

**Pocket mnemonic — C-B-T-K:**

| Letter | Layer | Means |
|---|---|---|
| **C** | Concession + Counterplay | What does normal cost? Is their counterplay dead before I grab? |
| **B** | Bind + second target | Restrict scope; if they fixed one weakness, open another |
| **T** | Trade down risk + Technique | Trade when their counterplay dies faster; name the endgame first |
| **K** | King walk | In simplified positions, centralize the king before pushing |

**Over the board:** run **C → B → T → K** top to bottom. If a heavier layer says *not yet*, a lighter principle does not override it.

### The stack (heavy at the bottom — build on these)

#### Bedrock ●●● — Ask every move — skip these and the rest is guessing

- **#1 Normal moves become concessions** — What does the opponent lose by defending normally?  
  *Corpus ~50% · Underlying lens — implicated in almost every forcing win*
- **#3 Deny counterplay before collecting** — List checks, breaks, trades before grabbing material  
  *Corpus ~51% · King pressure + passers co-occur in ~half of wins*

#### Beam ●●○ — Load-bearing in ~3–5 wins in ten — ask before tactics

- **#2 One weakness is not enough** — Fix one, then create or switch to a second target  
  *Corpus ~35% · Infiltration and restriction show up in ~3–4 wins in ten*
- **#5 Restriction before tactics** — Narrow piece scope; the tactic is the receipt, not the plan  
  *Corpus ~35% · Development edge tags ~28% of decisive wins*

#### Timber ●○○ — Decisive when ahead — about one win in four

- **#4 Conversion = lower risk, not just more material** — Trade when opponent counterplay dies faster than yours grows  
  *Corpus ~18% · Queen trades while ahead — ~1 win in 5 when already winning*
- **#6 Technique is part of the line** — After forcing play, name the endgame or mating net  
  *Corpus ~18% · Name the receipt before you cash — discipline, not a tactic label*

#### Key 🔑 — Light until simplified — then non-negotiable

- **#7 Activate the king before collecting** — In simplified positions, king moves are part of the winning line  
  *Corpus ~18% · Endgame-only — ~18% of wins, mandatory once simplified*

**One sentence to keep:** *Bedrock asks what normal costs; beam binds; timber names the receipt; the key turns only in the ending.*

---


## Candidate principles (watch for repeats)

| Principle | Command |
|---|---|
| Second-rank infiltration with dual passers | Occupy the second rank while distant passers advance; deny king counterplay before pushing. |
| King-file alignment after a central wedge | A central pawn wedge wins when the enemy king shares a file with your heavy pieces. |
| Seventh rank then pawn roller | Bind on the seventh rank, then roll a passer — checks force concessions, not the goal. |

Promote a candidate into the ranked table only after it survives two separate event types.

---

## Corpus evidence (623,110 decisive games)

Theme tags are **non-exclusive** heuristics (a game may carry several). Rates show how often each winning pattern appeared in the corpus — not a single cause label.

| Theme | Tagged games | Rate | Wisdom |
|---|---:|---:|---|
| `pawn_break` | 619,581 | 99.4% | Pawn breaks apply concession pressure when they open lines faster than defenders redeploy. |
| `material_swing` | 606,710 | 97.4% | A forcing sequence gained material because normal replies conceded. |
| `passed_pawn` | 324,194 | 52.0% | Passed pawns convert when counterplay is denied first. |
| `king_attack` | 311,632 | 50.0% | Checks force defensive concessions; conversion follows when the king loses shelter. |
| `seventh_rank` | 257,522 | 41.3% | Seventh-rank infiltration wins when the defender is overloaded. |
| `development_edge` | 174,628 | 28.0% | Restriction first: lead in development forces the opponent into passive defense. |
| `queen_trade_ahead` | 115,121 | 18.5% | Trading queens while ahead lowers risk — convert to a technical endgame. |
| `king_activation` | 111,993 | 18.0% | In simplified positions, the winning king centralizes before collecting. |
| `castled_first` | 17,494 | 2.8% | Safe king shelter lets you apply concession pressure on the other wing. |
| `back_rank` | 747 | 0.1% | Back-rank pressure converts when the enemy king lacks escape squares. |

**Reading the table:** `pawn_break` and `material_swing` rates are high because the heuristics fire on almost any forcing win — treat them as background noise. Focus on combinations that reinforce the promoted principles (king activation, queen trades while ahead, seventh-rank entry).

---

## Study questions (Layer A)

1. **Concession pressure:** What did the loser lose by playing normal moves?
2. **Conversion-delta:** How did each winner move make the advantage safer or more permanent?
3. **Counterplay:** What opponent checks/captures/breaks were denied before material was taken?
4. **Horizon:** Was there a quiet setup move depth-limited engines might undervalue?
5. **Portable lesson:** One sentence a human could apply without memorizing a line?

Board helper: `python tools/twic_game_board.py <pgn> --phases`
