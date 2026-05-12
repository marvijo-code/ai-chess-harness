# Strategy Lessons

Generated: 2026-05-12 13:06:36
Completed games scanned: 5

This file stores model-discovered concepts from self-play evidence. It must not be treated as memorized move answers.

## Concept Synthesis
- status: ok
- message: synthesized 7 generalized concepts from 60 new observations

## Discovered Concepts
- Punish forcing moves that leave the mover en prise; (confidence 0.96); trigger: A capture or check is considered, but the moved piece will be immediately capturable by a lower- or equal-cost recapture with no compensation.; value adjustment: Decrease move value strongly; add extra penalty if the moved piece is a major piece.
- Do not sac queen/rook for check without forced gain; (confidence 0.98); trigger: A checking move by queen or rook can be captured by the king or a simple defender and there is no forced tactical return.; value adjustment: Apply very large penalty; only allow if immediate forced mate or clear net material gain is detected.
- Pre-move recapture safety gate; (confidence 0.95); trigger: Before finalizing any forcing move, opponent has an immediate legal recapture of the moved piece in one ply.; value adjustment: Subtract value proportional to expected net material after the likely recapture sequence (at least one-ply horizon).
- Respect legal king captures in tactical resolution; (confidence 0.91); trigger: After giving check or trading near kings, a legal king capture of the attacking piece exists.; value adjustment: Strongly downweight lines that ignore king captures; upweight lines that avoid placing capturable attackers next to the king.
- Convert advantage by reducing volatility; (confidence 0.88); trigger: Already ahead materially (large positive edge) but considering sharp forcing trades or speculative checks.; value adjustment: Increase value of simplification and safety; penalize high-volatility tactics that risk hanging pieces or perpetual complications.
- Promotion-race urgency and blocker priority; (confidence 0.84); trigger: Either side has a pawn one step from promotion or an imminent promotion race.; value adjustment: Heavily reward moves that stop, check, or outrun promotion; penalize non-urgent moves that allow immediate promotion.
- Low-clock pragmatism; (confidence 0.8); trigger: Time remaining is low or game trend shows time-pressure collapse.; value adjustment: Prefer fast, safe, low-branching moves; reduce value of complex speculative tactics requiring deep calculation.

## Evidence For Reflection
- material_swing (28 evidence): the opponent reply caused a material balance drop
  evidence: game 1 ply 16 as Black move f6e4: opponent reply Bxe4 shifted material by 300 centipawns
  evidence: game 1 ply 22 as Black move f6g5: opponent reply Nxg5+ shifted material by 300 centipawns
- undefended_forcing_piece (15 evidence): a forcing move left the moved piece immediately capturable
  evidence: game 1 ply 16 as Black move f6e4: forcing move leaves n capturable by d3e4
  evidence: game 1 ply 34 as Black move e6a2: forcing move leaves b capturable by a1a2
- hanging_checking_piece (7 evidence): a checking move left the moved piece capturable by the enemy king
  evidence: game 1 ply 78 as Black move e2e6: checking r can be captured by king move(s): f6e6
  evidence: game 1 ply 106 as Black move d4d6: checking q can be captured by king move(s): e6d6
- failed_conversion (5 evidence): the learner had a material edge but did not win
  evidence: game 1 ply 107 as Black: highest material edge was at least 900 centipawns but result was 1/2-1/2
  evidence: game 2 ply 40 as White: highest material edge was at least 1000 centipawns but result was 0-1
- missed_king_capture (2 evidence): a legal king capture was available but another move was chosen
  evidence: game 1 ply 28 as Black move f7g6: legal king capture candidate(s) were available: h7g6
  evidence: game 3 ply 32 as Black move f8g8: legal king capture candidate(s) were available: h8g8
- time_loss (2 evidence): the learner lost on time
  evidence: game 2 ply 40 as White: lost on time after 40 plies
  evidence: game 5 ply 51 as Black: lost on time after 51 plies
- pawn_promotion_failure (1 evidence): the learner had a pawn one step from promotion but did not win
  evidence: game 1 ply 107 as Black: had a pawn one step from promotion but did not win
