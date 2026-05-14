# Strategy Lessons

Generated: 2026-05-14 18:09:04
Completed games scanned: 14

This file stores model-discovered concepts from self-play evidence. It must not be treated as memorized move answers.

## Concept Synthesis
- status: ok
- message: synthesized 3 generalized concepts from 5 new observations

## Discovered Concepts
- high_value_forcing_capture_needs_exit_or_cover; (confidence 0.93); trigger: a forcing capture by a rook/queen enters enemy territory and the capturing piece can be immediately challenged by a comparable or cheaper defender with no safe retreat or reinforcement; value adjustment: strongly penalize such forcing captures unless the moved piece is protected, has a safe follow-up square, or the recapture sequence still wins net material
- tactical_move_must_recheck_back_rank_and_hanging_piece; (confidence 0.86); trigger: a tactical piece move (especially knight/rook activation) is considered while a back-rank or long-diagonal defender remains loose after the move; value adjustment: penalize tactical activations that leave a previously defended piece or back-rank asset newly capturable; reward moves that first secure or over-defend that asset
- pawn_thrust_capture_should_require_recapture_resilience; (confidence 0.79); trigger: a pawn capture/thrust is forcing in the center or kingside but the pawn becomes instantly recapturable by multiple enemy units; value adjustment: moderately penalize these pawn actions unless they open lines with immediate tactical payoff or are backed by a favorable recapture tree

## Evidence For Reflection
- material_swing (642 evidence): the opponent reply caused a material balance drop
  evidence: game 1 ply 16 as Black move c8e6: opponent reply Bxe6 shifted material by 300 centipawns
  evidence: game 1 ply 22 as Black move c5d4: opponent reply Nxd4 shifted material by 300 centipawns
- undefended_forcing_piece (254 evidence): a forcing move left the moved piece immediately capturable
  evidence: game 2 ply 15 as White move e4d5: forcing move leaves P capturable by d8d5, f6d5
  evidence: game 3 ply 20 as Black move e5d4: forcing move leaves p capturable by f3d4, d1d4, c3d4
- hanging_checking_piece (110 evidence): a checking move left the moved piece capturable by the enemy king
  evidence: game 13 ply 22 as Black move h5h2: checking q can be captured by king move(s): g1h2
  evidence: game 14 ply 21 as White move d5f7: checking B can be captured by king move(s): e8f7
- time_loss (73 evidence): the learner lost on time
  evidence: game 5 ply 31 as Black: lost on time after 31 plies
  evidence: game 6 ply 34 as White: lost on time after 34 plies
- failed_conversion (63 evidence): the learner had a material edge but did not win
  evidence: game 6 ply 34 as White: highest material edge was at least 900 centipawns but result was 0-1
  evidence: game 11 ply 21 as Black: highest material edge was at least 1100 centipawns but result was 1-0
- missed_king_capture (42 evidence): a legal king capture was available but another move was chosen
  evidence: game 7 ply 32 as Black move e8f7: legal king capture candidate(s) were available: g8f7
  evidence: game 11 ply 18 as Black move f8f7: legal king capture candidate(s) were available: g8f7
- pawn_promotion_failure (14 evidence): the learner had a pawn one step from promotion but did not win
  evidence: game 38 ply 36 as White: had a pawn one step from promotion but did not win
  evidence: game 2 ply 168 as White: had a pawn one step from promotion but did not win
- repetition_draw (8 evidence): the learner drew by repetition
  evidence: game 1 ply 122 as Black: drew by repetition instead of changing the position
  evidence: game 4 ply 90 as White: drew by repetition instead of changing the position
- mate_loss (7 evidence): the learner lost by mate
  evidence: game 35 ply 37 as Black: lost by mate after 37 plies
  evidence: game 37 ply 59 as Black: lost by mate after 59 plies
