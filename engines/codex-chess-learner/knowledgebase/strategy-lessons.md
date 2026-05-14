# Strategy Lessons

Generated: 2026-05-14 17:06:40
Completed games scanned: 8

This file stores model-discovered concepts from self-play evidence. It must not be treated as memorized move answers.

## Concept Synthesis
- status: ok
- message: synthesized 4 generalized concepts from 9 new observations

## Discovered Concepts
- avoid_single-move_recapture_trades_when_not_ahead; (confidence 0.96); trigger: a capture move can be immediately recaptured by an equal or lower-value enemy piece, and the sequence does not win net material or improve king safety; value adjustment: penalize such captures; reward alternatives that keep material tension without offering an immediate equalizing recapture
- preserve_defenders_before_center_pawn_pushes; (confidence 0.9); trigger: a pawn advance/capture in the center removes protection from a key square or piece, allowing an enemy piece to win material immediately; value adjustment: penalize center pawn pushes that undefend tactical points; reward preparatory moves that maintain defenders before pawn contact
- do_not_place_minor_pieces_on_direct_queen_capture_lines; (confidence 0.88); trigger: a bishop or knight move lands on a square directly capturable by the enemy queen with no compensating threat; value adjustment: heavily penalize such placements; reward squares where the minor piece is defended or where queen capture is tactically impossible
- queen_intrusion_emergency_trade_or_block; (confidence 0.94); trigger: an enemy queen penetrates deep (especially near first-rank/king-zone files) with direct capture or mate threats; value adjustment: strongly reward immediate queen trade, forced block, or king-flight resources; heavily penalize quiet rook redeployments that leave mating/capture threats alive

## Evidence For Reflection
- material_swing (605 evidence): the opponent reply caused a material balance drop
  evidence: game 1 ply 16 as Black move c8e6: opponent reply Bxe6 shifted material by 300 centipawns
  evidence: game 1 ply 22 as Black move c5d4: opponent reply Nxd4 shifted material by 300 centipawns
- undefended_forcing_piece (246 evidence): a forcing move left the moved piece immediately capturable
  evidence: game 2 ply 15 as White move e4d5: forcing move leaves P capturable by d8d5, f6d5
  evidence: game 3 ply 20 as Black move e5d4: forcing move leaves p capturable by f3d4, d1d4, c3d4
- hanging_checking_piece (105 evidence): a checking move left the moved piece capturable by the enemy king
  evidence: game 13 ply 22 as Black move h5h2: checking q can be captured by king move(s): g1h2
  evidence: game 14 ply 21 as White move d5f7: checking B can be captured by king move(s): e8f7
- time_loss (73 evidence): the learner lost on time
  evidence: game 5 ply 31 as Black: lost on time after 31 plies
  evidence: game 6 ply 34 as White: lost on time after 34 plies
- failed_conversion (62 evidence): the learner had a material edge but did not win
  evidence: game 6 ply 34 as White: highest material edge was at least 900 centipawns but result was 0-1
  evidence: game 11 ply 21 as Black: highest material edge was at least 1100 centipawns but result was 1-0
- missed_king_capture (38 evidence): a legal king capture was available but another move was chosen
  evidence: game 7 ply 32 as Black move e8f7: legal king capture candidate(s) were available: g8f7
  evidence: game 11 ply 18 as Black move f8f7: legal king capture candidate(s) were available: g8f7
- pawn_promotion_failure (13 evidence): the learner had a pawn one step from promotion but did not win
  evidence: game 38 ply 36 as White: had a pawn one step from promotion but did not win
  evidence: game 2 ply 168 as White: had a pawn one step from promotion but did not win
- mate_loss (7 evidence): the learner lost by mate
  evidence: game 35 ply 37 as Black: lost by mate after 37 plies
  evidence: game 37 ply 59 as Black: lost by mate after 59 plies
- repetition_draw (5 evidence): the learner drew by repetition
  evidence: game 1 ply 122 as Black: drew by repetition instead of changing the position
  evidence: game 4 ply 90 as White: drew by repetition instead of changing the position
