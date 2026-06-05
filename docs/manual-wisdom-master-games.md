# chess-wisdom — manual TWIC master-game research

Updated: 2026-06-05  
Source prompt: [manual-wisdom-prompt.md](manual-wisdom-prompt.md)  
Checklist: [manual-wisdom-checklist.md](manual-wisdom-checklist.md)  
Latest TWIC anchor: **twic1647g.zip** (TWIC 1647, 2026-06-01)

---

## Method (honest)

| Layer | Role |
|---|---|
| **A — Human/LLM study** | Board-verified study: python-chess + FEN at phase boundaries, then concession-pressure / conversion-delta notes |
| **B — Bulk parser** (`tools/manual_twic_wisdom.py`) | Download/count coverage only; does **not** replace Layer A |

**Layer A rule:** step the board before structural claims — PGN text alone is not enough.  
Helper: `python tools/twic_game_board.py <pgn> --phases`

**Locked settings (Phase 0):** wisdom doc only · stop at **2025-01-01** · **10 games/step** · decisive + **both ≥ 2500 Elo**

---


## Batch mode (active)

Updated: 2026-06-05T11:48:10Z

| Field | Value |
|---|---|
| Mode | Automated — all decisive games, board-verified heuristics |
| TWIC issues done | 20 / 75 |
| Decisive games analyzed | 102,602 |
| Issue range (in progress) | 1647 → 1628 |
| Per-issue log | [manual-wisdom-twic-ledger.md](manual-wisdom-twic-ledger.md) |
| Target cutoff | 2025-01-01 |

---

## Principles (promoted so far)

| Rank | Principle | Command |
|---|---|---|
| 1 | Normal moves become concessions | What does the opponent lose by defending normally? |
| 2 | One weakness is not enough | Fix one, then create or switch to a second target |
| 3 | Deny counterplay before collecting | List checks, breaks, trades before grabbing material |
| 4 | Conversion = lower risk, not just more material | Trade when opponent counterplay dies faster than yours grows |
| 5 | Restriction before tactics | Narrow piece scope; the tactic is the receipt, not the plan |
| 6 | Technique is part of the line | After forcing play, name the endgame or mating net |
| 7 | Activate the king before collecting | In simplified positions, king moves are part of the winning line |

**Formula:** Win = concession pressure × conversion delta × counterplay denial

---

## Coverage ledger

| Field | Value |
|---|---|
| Latest TWIC studied (Layer A) | **1647** — step 2 of N |
| Games studied this session | **20 / 224** eligible (≥2500, decisive) in TWIC 1647 |
| Bulk coverage (Layer B, optional) | 75 issues, 1647→1573, ~537k games parsed |
| Target cutoff | 2025-01-01 |
| Next step | Step 3 — next 10 from 1647 (204 remaining) |

---

## 2026-06-04 — TWIC 1647 — Step 1 (10 games)

### 1. Yoo,C — Demchenko,A (0-1) · Titled Tue · Sicilian closed

**Concession pressure:** White's `Nxd5` and queen trades opened the b-file and fixed white's king on g1/h1 while black's rook reached the third rank. Each "equal" recapture worsened white's pawn shape (`bxb3`, isolated d-pawn).

**Conversion-delta:** Queen trade → rook + passer endgame where black's king walks forward (`Kg5–Kh4`) and white's rook is passive. Risk dropped every time white's pawns became targets.

**Lesson:** Pawn breaks that open a file toward the enemy king are winning science when the opponent's pieces are already committed elsewhere.  
**New vs prior:** reinforces #1, #5  
**Next:** compare with other b-pawn break wins in this batch

---

### 2. Hakobyan,A — Gumularz,S (0-1) · Stepan Avagyan Mem · English

**Concession pressure:** `…Ba3` and `Rc7` forced white's queen away from defending the back rank. White's `Qxa3` accepted a permanently weak first rank.

**Conversion-delta:** Trade into queen endgame → rook + distant passer (`…gxh4`, king march). Counterplay on c-file was denied before black collected.

**Lesson:** Seventh-rank infiltration wins when the defender's queen is the only piece holding the rank — overload the defender, not the square.  
**New vs prior:** reinforces #2 (second target: passer after rank weakness)  
**Next:** watch for Ba3/Ra7 pattern in classical games

---

### 3. Daneshvar,B — Razafindratsima,T (0-1) · Titled Tue · Sicilian closed

**Concession pressure:** `…e6` pawn break destroyed white's kingside shelter after white over-extended with `f5`/`Rh3`. Black's bishop on b4 tied down the queen.

**Conversion-delta:** `Ng5+` / `Nxh7` net — king hunt became mate because white's pieces could not return in time.

**Lesson:** A pawn break applies concession pressure when it opens lines faster than defenders can redeploy.  
**New vs prior:** reinforces #3 (deny counterplay before mate)  
**Next:** note f5/Rh3 overextension as loser pattern to avoid learning

---

### 4. Wadsworth,M — Le Tuan Minh (0-1) · 3-0 Thu · QBG

**Concession pressure:** Black grabbed on h1 but the real concession was white's scattered pawns and advancing `…d3` passer. Each white piece recapture lost tempo.

**Conversion-delta:** Bishop pair + king walk in a simplified ending — counterplay on g-file ended, then d-pawn decided.

**Lesson:** In chaos, the side that fixes one passed pawn and activates the king converts; material lead without king plan is false safety.  
**New vs prior:** new emphasis on **king activation in conversion**  
**Next:** promote if seen again in step 2

---

### 5. Carlsen,M — Keymer,V (1-0) · Norway Armageddon · Larsen

**Concession pressure:** White fixed black's structure (`b5`, `Nc6` fork trick), traded into a bind on the c-file, and restricted black's knight hops before winning the exchange on g8.

**Conversion-delta:** Exchange up + fixed pawns → black's counterplay on g-file was illusory; white's king marched safely.

**Lesson:** Bind first, collect second — Armageddon rewards patience over premature tactics.  
**New vs prior:** reinforces #5 strongly  
**Next:** compare with Firouzja bind in game 6

---

### 6. Firouzja,A — Keymer,V (1-0) · Norway Armageddon · English

**Concession pressure:** `Ra8+` and `e6` break forced black's king to the back rank while white's rook dominated the seventh. Trades on c5/d5 forced concessions from black's coordination.

**Conversion-delta:** Rook endgame with active rook + `f6`/`f7` promotion net — each check reduced black's drawing resources.

**Lesson:** Seventh-rank entry plus pawn roller is a conversion chain; checks force defensive concessions, not the goal.  
**New vs prior:** reinforces #4, #6  
**Next:** pair with Carlsen game as Norway double-example

---

### 7. Xue,H — Rustemov,A (1-0) · 3-0 Thu · English

**Concession pressure:** `f5` break opened the king; black's pieces on e6/f6 were overloaded defending both king and e-pawn.

**Conversion-delta:** `Qf7` / `Rxd8` — queen invasion with rook support; black's counterplay never started.

**Lesson:** Central pawn breaks win when enemy minor pieces face two jobs at once.  
**New vs prior:** reinforces #2 (overload = second weakness)  
**Next:** —

---

### 8. Andreikin,D — Nguyen Ngoc Truong Son (1-0) · 3-0 Thu · QGD

**Concession pressure:** `g4–g5–g6` fixed black's kingside; black knights had no stable outpost while white opened the g-file.

**Conversion-delta:** Sacrifice on h5 → `gxf7+` exposed king → rook lift `Rdg1` finished before black could untangle.

**Lesson:** Pawn storm is winning science only after enemy pieces are restricted — here knights were misplaced first.  
**New vs prior:** reinforces #5, #3  
**Next:** —

---

### 9. Zhu,J — Koneru,H (0-1) · Norway Armageddon w · Reti

**Concession pressure:** `…d4` wedge split white's army; `Rc8+` exploited white's king on c1 with queen vs rook coordination.

**Conversion-delta:** Trade on c3 removed defender; black's bishop on d5 dominated the open position.

**Lesson:** A central pawn wedge applies concession pressure when the enemy king is on the same file as rooks.  
**New vs prior:** new — **king-file alignment after wedge**  
**Next:** promote if repeated in step 2

---

### 10. Erigaisi,A — Vokhidov,S (1-0) · Titled Tue · QPG

**Concession pressure:** `Bg6` and `Rf4` tied down black's queen; `Ne5` trades opened the f-file toward the king.

**Conversion-delta:** `Rf8+` → `Qxf8+` → `Qg8#` — mating net defined early; black's queen had no safe square.

**Lesson:** When the enemy queen is the only defender, lift rooks to the back rank before collecting material.  
**New vs prior:** reinforces #6 (mate net named before final capture)  
**Next:** —

---

## Step 1 synthesis

| Theme | Count in 10 games | Status |
|---|---|---|
| Restriction/bind before tactics | 6 | reinforces #5 |
| Pawn break opens king or file | 5 | reinforces #1 |
| Second weakness / overload | 4 | reinforces #2 |
| Endgame king + passer conversion | 3 | candidate promote |
| King-file alignment after wedge | 1 | watch step 2 |

**Candidate new principle:** *Activate the king before collecting in simplified positions* (game 4).

---

## 2026-06-05 — TWIC 1647 — Step 2 (10 games)

**Hypothesis going in:** Step 1 themes (restriction, pawn breaks, king activation) should repeat; watch for back-rank / second-rank infiltration and whether king walks appear again.

### 1. Kosakowski,J — Bortnyk,O (1-0) · Titled Tue · English

**Concession pressure:** White's `Rxe7` and piece trades left Black's king exposed on the kingside; each recapture fixed Black's pieces on passive squares while White's bishop pair dominated open diagonals.

**Conversion-delta:** Queen trade → bishop endgame → `Kg5–Kf3` king march with g-passer and c-passer; risk fell as Black's rook stayed passive on the back rank.

**Lesson:** In bishop endings, the side that activates the king first converts even from modest material edges.  
**New vs prior:** **promotes #7** (king activation)  
**Next:** —

---

### 2. Pranav,V — Gustafsson,J (1-0) · Titled Tue · Vienna

**Concession pressure:** `f4` break and `Bf7+` forced Black's queen off defense; each natural reply lost king safety (`Qxf7`, king hunt).

**Conversion-delta:** `Qd5+` → `Bg5+` → `Qf5+` chain left Black's king in the center with no counterplay.

**Lesson:** Forcing sequences that expose the enemy king convert faster than quiet accumulation — but the setup (`f4`, piece placement) still restricted Black first.  
**New vs prior:** reinforces #5, #3  
**Next:** —

---

### 3. Nakamura,H — Ibarra Jerez,JC (1-0) · Titled Tue · Old Indian

**Concession pressure:** `Rxh5` sac opened the h-file; Black's `Qxf5` accepted a shattered kingside while White's pieces flooded in via `Nf5`, `Bxe4`.

**Conversion-delta:** `Rf6` + `Qg6+` mating net — Black's king had no shelter after the file opened.

**Lesson:** Exchange sacrifices on the kingside apply concession pressure when the enemy king lacks a pawn buffer and pieces cannot retreat in time.  
**New vs prior:** reinforces #1, #6  
**Next:** —

---

### 4. Gumularz,S — Aravindh,Chithambaram VR. (1-0) · Avagyan Mem · QGD

**Concession pressure:** `Qxf8+` trade and `Ra7` tied Black's rooks to defense; `f5` break opened the h-file before Black could reorganize.

**Conversion-delta:** `Rxh7` with Black's king stuck on f8 — restriction on the back rank converted to decisive material.

**Lesson:** Seventh-rank entry combined with a pawn break is a two-step conversion: bind, then invade.  
**New vs prior:** reinforces #2, #5 (pairs with Hakobyan game from step 1)  
**Next:** —

---

### 5. Bjerre,J — Donchenko,A (0-1) · Titled Tue · Sicilian

**Concession pressure:** `Ne4+` fork and `…c5` break split White's coordination; queen trades left White's king exposed on the kingside.

**Conversion-delta:** Endgame `Kg6–Kf5–Kg3` king walk plus h-passer — White's rook could not stop both.

**Lesson:** After forcing trades, king activation decides — the side that centralizes the king first wins rook endings with passers.  
**New vs prior:** **promotes #7**  
**Next:** —

---

### 6. Drygalov,S — Rustemov,A (0-1) · 3-0 Thu · QGD

**Concession pressure:** `…g4`/`…h5` fixed White's kingside; bishop pair on c5/e5 dominated while White's king stayed on b1/c1.

**Conversion-delta:** `…Re1`/`…Re2` second-rank infiltration → `…a3`/`…a2`/`…h2` passers with king march `…Kg3`.

**Lesson:** Second-rank occupation plus distant passers is a conversion chain — deny the enemy king counterplay before pushing passers.  
**New vs prior:** new — **second-rank infiltration + dual passers**  
**Next:** watch step 3 for repeat

---

### 7. Assaubayeva,B — Zhu,J (1-0) · Norway Chess Women · QGD

**Concession pressure:** Early queen trade and `Nd4+` fork won the exchange; Black's pieces were uncoordinated on the back rank.

**Conversion-delta:** `Kc3` centralization held against `…Rc8+`; White's active king supported the final invasion.

**Lesson:** In simplified positions, centralize your king before the opponent's rooks become active.  
**New vs prior:** reinforces #7  
**Next:** —

---

### 8. Muzychuk,A — Koneru,H (0-1) · Norway Armageddon w · Larsen

**Concession pressure:** `…gxh3`/`…Ng4+` opened the king; White's king walk `Kg2–Kh2` could not escape the mating net.

**Conversion-delta:** `…Bh5+` finished a king hunt where White's queen had no safe square.

**Lesson:** Pawn storms on the g-file apply concession pressure when the enemy king lacks flight squares.  
**New vs prior:** reinforces #1, #6 (pairs with Andreikin g-storm from step 1)  
**Next:** —

---

### 9. Aravindh,Chithambaram VR. — Donchenko,A (0-1) · Avagyan Mem · Reti

**Concession pressure:** `…f6+` break shattered White's pawn cover; Black's queen picked off pawns while White's king stayed passive on g2.

**Conversion-delta:** `…Kd7` centralization in a queen endgame — Black's king supported the final mop-up.

**Lesson:** Pawn-break concessions in the center often win because they fix the enemy king while your queen stays mobile.  
**New vs prior:** reinforces #7, #1  
**Next:** —

---

### 10. Martirosyan,H — Korobov,A (0-1) · Titled Tue · Sicilian Taimanov

**Concession pressure:** `…Ra4` raider and `…Rh4` tied White down; `…Rxg6+` king hunt exploited White's exposed king on e2.

**Conversion-delta:** `…Ra2+` back-rank finish — White's king had no escape after the g-file opened.

**Lesson:** Raider rook on a4/a5 applies concession pressure by forcing defensive concessions, then switch to king hunt or back rank.  
**New vs prior:** reinforces #2 (second target: back rank after king hunt)  
**Next:** —

---

## Step 2 synthesis

| Theme | Count in 10 games | Status |
|---|---|---|
| King activation in conversion | 5 | **#7 promoted** |
| Restriction/bind before tactics | 5 | reinforces #5 |
| Kingside file opening / pawn storm | 4 | reinforces #1 |
| Second-rank / back-rank infiltration | 3 | candidate (game 6) |
| Seventh-rank entry | 2 | reinforces step 1 |

**New candidate:** *Second-rank infiltration with dual passers* (game 6) — promote if seen again in step 3.

---

## Next step (waiting on you)

After you review step 2, choose:

1. **More from 1647** — next 10 elite decisive games (204 remaining in pool)  
2. **Advance to 1646** — fresh issue, same 10-game pace  
3. **Adjust filter** — e.g. Norway/classical only, higher Elo floor

Say **1**, **2**, **3**, or give edits to principles — then we proceed.
