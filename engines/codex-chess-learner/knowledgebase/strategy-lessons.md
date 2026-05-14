# Strategy Lessons

Generated: 2026-05-13 12:27:31
Completed games scanned: 2

This file stores model-discovered concepts from self-play evidence. It must not be treated as memorized move answers.

## Concept Synthesis
- status: unchanged
- message: no new self-play evidence

## Discovered Concepts
- checking_capture_must_survive_reply; (confidence 0.97); trigger: a checking or forcing capture places the attacking piece on a square where the opponent king or a nearby defender can immediately recapture safely; value adjustment: strongly penalize the forcing move unless it wins clear net material or creates a forced continuation; scale penalty by sacrificed piece value and recapture certainty
- high_value_capture_requires_recapture_audit; (confidence 0.93); trigger: a queen/rook capture enters a contested square with immediate opponent recapture available; value adjustment: apply a pre-move net-material checkpoint: penalize entries where best opponent recapture erases the gain; reward alternatives that keep the high-value piece defended or preserve the gain after one full reply cycle
- quiet_development_needs_tactical_safety_gate; (confidence 0.84); trigger: a non-forcing developing move leaves a piece or central point immediately capturable by a lower-risk opponent reply; value adjustment: penalize development that increases immediate tactical liability; reward preparatory moves that add defenders, reduce attacker access, or delay development until the target is protected
- when_ahead_and_low_on_time_choose_low_variance_lines; (confidence 0.9); trigger: side has a measurable material edge but remaining clock is low or sequence complexity is rising; value adjustment: increase preference for fast-to-verify, material-preserving moves and simplifications; penalize speculative forcing lines and sacrificial checks that require deep calculation

## Evidence For Reflection
- material_swing (418 evidence): the opponent reply caused a material balance drop
  evidence: game 1 ply 16 as Black move c8e6: opponent reply Bxe6 shifted material by 300 centipawns
  evidence: game 1 ply 22 as Black move c5d4: opponent reply Nxd4 shifted material by 300 centipawns
- undefended_forcing_piece (180 evidence): a forcing move left the moved piece immediately capturable
  evidence: game 2 ply 15 as White move e4d5: forcing move leaves P capturable by d8d5, f6d5
  evidence: game 3 ply 20 as Black move e5d4: forcing move leaves p capturable by f3d4, d1d4, c3d4
- time_loss (70 evidence): the learner lost on time
  evidence: game 5 ply 31 as Black: lost on time after 31 plies
  evidence: game 6 ply 34 as White: lost on time after 34 plies
- failed_conversion (43 evidence): the learner had a material edge but did not win
  evidence: game 6 ply 34 as White: highest material edge was at least 900 centipawns but result was 0-1
  evidence: game 11 ply 21 as Black: highest material edge was at least 1100 centipawns but result was 1-0
- hanging_checking_piece (27 evidence): a checking move left the moved piece capturable by the enemy king
  evidence: game 13 ply 22 as Black move h5h2: checking q can be captured by king move(s): g1h2
  evidence: game 14 ply 21 as White move d5f7: checking B can be captured by king move(s): e8f7
- missed_king_capture (10 evidence): a legal king capture was available but another move was chosen
  evidence: game 7 ply 32 as Black move e8f7: legal king capture candidate(s) were available: g8f7
  evidence: game 11 ply 18 as Black move f8f7: legal king capture candidate(s) were available: g8f7
- mate_loss (3 evidence): the learner lost by mate
  evidence: game 35 ply 37 as Black: lost by mate after 37 plies
  evidence: game 37 ply 59 as Black: lost by mate after 59 plies
- pawn_promotion_failure (1 evidence): the learner had a pawn one step from promotion but did not win
  evidence: game 38 ply 36 as White: had a pawn one step from promotion but did not win
