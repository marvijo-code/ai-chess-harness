# Strategy Lessons

Generated: 2026-05-14 18:30:18
Completed games scanned: 16

This file stores model-discovered concepts from self-play evidence. It must not be treated as memorized move answers.

## Concept Synthesis
- status: ok
- message: synthesized 4 generalized concepts from 11 new observations

## Discovered Concepts
- do_not_chain_forcing_captures_with_same_piece_without_exit_square; (confidence 0.95); trigger: the same attacking piece makes consecutive forcing captures/checks while each landing square can be immediately recaptured by an enemy pawn or piece; value adjustment: strongly penalize repeated forcing continuations unless the final square is defended or wins clear net material within the next reply cycle
- post_move_looseness_check_on_moved_piece; (confidence 0.91); trigger: a move improves activity but the moved piece loses prior protection or enters a square with equal-or-cheaper recapture available to the opponent; value adjustment: penalize activity moves that create a loose moved piece; reward alternatives that keep at least one stable defender on the moved unit
- promotion_threat_priority_over_non_forcing_improvements; (confidence 0.97); trigger: an enemy pawn is one step from promotion or has an unobstructed promotion path, and candidate moves do not directly stop promotion; value adjustment: apply a very large penalty to non-stopping moves; heavily reward moves that prevent promotion immediately (block, capture, or force king/rook control)
- convert_large_advantage_by_avoiding_repeat_cycles; (confidence 0.93); trigger: side has a large material edge but candidate move repeats prior position without increasing king safety, promotion control, or simplification quality; value adjustment: increase penalty for repetition when materially ahead; reward safe conversion features such as trade-down, passed-pawn control, and king activation

## Evidence For Reflection
- material_swing (651 evidence): the opponent reply caused a material balance drop
  evidence: game 1 ply 16 as Black move c8e6: opponent reply Bxe6 shifted material by 300 centipawns
  evidence: game 1 ply 22 as Black move c5d4: opponent reply Nxd4 shifted material by 300 centipawns
- undefended_forcing_piece (259 evidence): a forcing move left the moved piece immediately capturable
  evidence: game 2 ply 15 as White move e4d5: forcing move leaves P capturable by d8d5, f6d5
  evidence: game 3 ply 20 as Black move e5d4: forcing move leaves p capturable by f3d4, d1d4, c3d4
- hanging_checking_piece (110 evidence): a checking move left the moved piece capturable by the enemy king
  evidence: game 13 ply 22 as Black move h5h2: checking q can be captured by king move(s): g1h2
  evidence: game 14 ply 21 as White move d5f7: checking B can be captured by king move(s): e8f7
- time_loss (73 evidence): the learner lost on time
  evidence: game 5 ply 31 as Black: lost on time after 31 plies
  evidence: game 6 ply 34 as White: lost on time after 34 plies
- failed_conversion (64 evidence): the learner had a material edge but did not win
  evidence: game 6 ply 34 as White: highest material edge was at least 900 centipawns but result was 0-1
  evidence: game 11 ply 21 as Black: highest material edge was at least 1100 centipawns but result was 1-0
- missed_king_capture (42 evidence): a legal king capture was available but another move was chosen
  evidence: game 7 ply 32 as Black move e8f7: legal king capture candidate(s) were available: g8f7
  evidence: game 11 ply 18 as Black move f8f7: legal king capture candidate(s) were available: g8f7
- pawn_promotion_failure (14 evidence): the learner had a pawn one step from promotion but did not win
  evidence: game 38 ply 36 as White: had a pawn one step from promotion but did not win
  evidence: game 2 ply 168 as White: had a pawn one step from promotion but did not win
- repetition_draw (10 evidence): the learner drew by repetition
  evidence: game 1 ply 122 as Black: drew by repetition instead of changing the position
  evidence: game 4 ply 90 as White: drew by repetition instead of changing the position
- mate_loss (7 evidence): the learner lost by mate
  evidence: game 35 ply 37 as Black: lost by mate after 37 plies
  evidence: game 37 ply 59 as Black: lost by mate after 59 plies
