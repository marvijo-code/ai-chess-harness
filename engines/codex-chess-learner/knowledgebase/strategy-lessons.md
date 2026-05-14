# Strategy Lessons

Generated: 2026-05-14 17:41:09
Completed games scanned: 11

This file stores model-discovered concepts from self-play evidence. It must not be treated as memorized move answers.

## Concept Synthesis
- status: ok
- message: synthesized 6 generalized concepts from 11 new observations

## Discovered Concepts
- avoid_forcing_captures_that_lose_the_mover_without_net_gain; (confidence 0.97); trigger: a forcing capture or check places the moved piece on a square where immediate recapture is available and no clear material/king-safety compensation follows; value adjustment: strongly penalize such forcing moves; reward alternatives that keep the attacker protected or delay the tactic until support is added
- stabilize_overloaded_pieces_before_second_tactical_jump; (confidence 0.9); trigger: one tactical piece has already moved into contested territory and candidate continuation asks the same side to make another aggressive jump without resolving current tactical liabilities; value adjustment: penalize chaining aggressive piece jumps when defenders are insufficient; reward consolidating moves that secure threatened pieces first
- treat_enemy_pawn_on_7th_as_emergency_before_rook_activity; (confidence 0.98); trigger: opponent has a pawn one move from promotion and candidate move is a non-forcing rook/tempo move that does not stop promotion; value adjustment: heavily penalize non-preventive rook activity; reward blockade, capture, or forcing checks that delay/neutralize promotion
- after_forced_king_recapture_scan_for_hanging_back_rank_assets; (confidence 0.84); trigger: king is drawn into a forced recapture line and remaining heavy pieces/pawns on adjacent files become newly loose; value adjustment: penalize king recaptures that permit immediate high-value follow-up captures unless compensated; reward lines that keep post-recapture structure defended
- endgame_king_should_prefer_safe_material_gain_over_neutral_wait; (confidence 0.88); trigger: in reduced-material endgames, the king has a legal safe capture that removes enemy material or active piece support; value adjustment: reward taking safe king captures immediately; penalize neutral king shuffles that pass on free gain
- anti_repetition_when_static_eval_is_not_worse; (confidence 0.78); trigger: position repetition is available while legal alternatives can change pawn structure, king activity, or rook placement without immediate tactical loss; value adjustment: penalize repeating moves in equal-or-better states; reward safe position-changing plans that create new winning chances

## Evidence For Reflection
- material_swing (629 evidence): the opponent reply caused a material balance drop
  evidence: game 1 ply 16 as Black move c8e6: opponent reply Bxe6 shifted material by 300 centipawns
  evidence: game 1 ply 22 as Black move c5d4: opponent reply Nxd4 shifted material by 300 centipawns
- undefended_forcing_piece (250 evidence): a forcing move left the moved piece immediately capturable
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
- mate_loss (7 evidence): the learner lost by mate
  evidence: game 35 ply 37 as Black: lost by mate after 37 plies
  evidence: game 37 ply 59 as Black: lost by mate after 59 plies
- repetition_draw (7 evidence): the learner drew by repetition
  evidence: game 1 ply 122 as Black: drew by repetition instead of changing the position
  evidence: game 4 ply 90 as White: drew by repetition instead of changing the position
