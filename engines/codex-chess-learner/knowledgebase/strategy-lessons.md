# Strategy Lessons

Generated: 2026-05-14 15:50:09
Completed games scanned: 16

This file stores model-discovered concepts from self-play evidence. It must not be treated as memorized move answers.

## Concept Synthesis
- status: ok
- message: synthesized 5 generalized concepts from 21 new observations

## Discovered Concepts
- forcing_capture_must_not_drop_the_attacker; (confidence 0.96); trigger: a capture or forcing move lands a piece on a square with immediate low-cost recapture and no compensating gain; value adjustment: penalize these forcing moves; reward alternatives that keep the attacker defended or improve recapture balance first
- high_value_piece_trade_square_safety; (confidence 0.94); trigger: queen or rook captures onto a contested square where an equal or lower-cost immediate recapture is available; value adjustment: strongly penalize entering contested capture squares with high-value pieces unless follow-up secures net gain
- checking_move_requires_checker_survival; (confidence 0.99); trigger: a checking move leaves the checking piece capturable by the king on the next move; value adjustment: strongly penalize such checks unless the king capture is illegal or loses decisive material immediately
- king_capture_opportunism_in_simplified_positions; (confidence 0.82); trigger: in reduced-material positions, the king has a legal safe capture that removes an active enemy unit; value adjustment: increase priority for safe king captures; penalize king moves that ignore immediate safe capture gains
- low_clock_conversion_mode; (confidence 0.9); trigger: long game with advantage where clock risk grows and tactical precision demands are high; value adjustment: shift value toward faster, simpler, low-blunder moves (safe exchanges, direct promotion support, immediate threat neutralization); penalize slow speculative maneuvering

## Evidence For Reflection
- material_swing (542 evidence): the opponent reply caused a material balance drop
  evidence: game 1 ply 16 as Black move c8e6: opponent reply Bxe6 shifted material by 300 centipawns
  evidence: game 1 ply 22 as Black move c5d4: opponent reply Nxd4 shifted material by 300 centipawns
- undefended_forcing_piece (225 evidence): a forcing move left the moved piece immediately capturable
  evidence: game 2 ply 15 as White move e4d5: forcing move leaves P capturable by d8d5, f6d5
  evidence: game 3 ply 20 as Black move e5d4: forcing move leaves p capturable by f3d4, d1d4, c3d4
- hanging_checking_piece (80 evidence): a checking move left the moved piece capturable by the enemy king
  evidence: game 13 ply 22 as Black move h5h2: checking q can be captured by king move(s): g1h2
  evidence: game 14 ply 21 as White move d5f7: checking B can be captured by king move(s): e8f7
- time_loss (73 evidence): the learner lost on time
  evidence: game 5 ply 31 as Black: lost on time after 31 plies
  evidence: game 6 ply 34 as White: lost on time after 34 plies
- failed_conversion (56 evidence): the learner had a material edge but did not win
  evidence: game 6 ply 34 as White: highest material edge was at least 900 centipawns but result was 0-1
  evidence: game 11 ply 21 as Black: highest material edge was at least 1100 centipawns but result was 1-0
- missed_king_capture (32 evidence): a legal king capture was available but another move was chosen
  evidence: game 7 ply 32 as Black move e8f7: legal king capture candidate(s) were available: g8f7
  evidence: game 11 ply 18 as Black move f8f7: legal king capture candidate(s) were available: g8f7
- pawn_promotion_failure (9 evidence): the learner had a pawn one step from promotion but did not win
  evidence: game 38 ply 36 as White: had a pawn one step from promotion but did not win
  evidence: game 2 ply 168 as White: had a pawn one step from promotion but did not win
- mate_loss (4 evidence): the learner lost by mate
  evidence: game 35 ply 37 as Black: lost by mate after 37 plies
  evidence: game 37 ply 59 as Black: lost by mate after 59 plies
- repetition_draw (4 evidence): the learner drew by repetition
  evidence: game 1 ply 122 as Black: drew by repetition instead of changing the position
  evidence: game 4 ply 90 as White: drew by repetition instead of changing the position
