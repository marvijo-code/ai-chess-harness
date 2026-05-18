# Strategy Lessons

Generated: 2026-05-18 11:27:58
Completed games scanned: 13

This file stores model-discovered concepts from self-play evidence. It must not be treated as memorized move answers.

## Concept Synthesis
- status: deferred
- message: concept synthesis deferred while live training is running

## Discovered Concepts
- recapture_only_if_structure_does_not_uncover_immediate_piece_loss; (confidence 0.69); trigger: a center recapture/capture with a pawn or minor piece changes protection lines for own developed pieces that are already candidate targets; value adjustment: penalize the recapture if it removes or weakens defenders and allows an immediate favorable opponent pickup/exchange; reward alternatives that keep the target piece defended before recapturing
- forcing_minor_capture_needs_global_loose_piece_scan; (confidence 0.9); trigger: a forcing knight capture is considered and either the moved knight or another nearby developed piece becomes attackable in one move; value adjustment: strongly penalize the forcing capture unless both the moved piece and other exposed pieces remain tactically covered after the opponent�s best immediate reply; reward consolidating moves that remove loose-piece vulnerabilities first

## Evidence For Reflection
- material_swing (828 evidence): the opponent reply caused a material balance drop
  evidence: game 1 ply 16 as Black move c8e6: opponent reply Bxe6 shifted material by 300 centipawns
  evidence: game 1 ply 22 as Black move c5d4: opponent reply Nxd4 shifted material by 300 centipawns
- undefended_forcing_piece (340 evidence): a forcing move left the moved piece immediately capturable
  evidence: game 2 ply 15 as White move e4d5: forcing move leaves P capturable by d8d5, f6d5
  evidence: game 3 ply 20 as Black move e5d4: forcing move leaves p capturable by f3d4, d1d4, c3d4
- hanging_checking_piece (114 evidence): a checking move left the moved piece capturable by the enemy king
  evidence: game 13 ply 22 as Black move h5h2: checking q can be captured by king move(s): g1h2
  evidence: game 14 ply 21 as White move d5f7: checking B can be captured by king move(s): e8f7
- time_loss (79 evidence): the learner lost on time
  evidence: game 5 ply 31 as Black: lost on time after 31 plies
  evidence: game 6 ply 34 as White: lost on time after 34 plies
- failed_conversion (75 evidence): the learner had a material edge but did not win
  evidence: game 6 ply 34 as White: highest material edge was at least 900 centipawns but result was 0-1
  evidence: game 11 ply 21 as Black: highest material edge was at least 1100 centipawns but result was 1-0
- missed_king_capture (64 evidence): a legal king capture was available but another move was chosen
  evidence: game 7 ply 32 as Black move e8f7: legal king capture candidate(s) were available: g8f7
  evidence: game 11 ply 18 as Black move f8f7: legal king capture candidate(s) were available: g8f7
- pawn_promotion_failure (19 evidence): the learner had a pawn one step from promotion but did not win
  evidence: game 38 ply 36 as White: had a pawn one step from promotion but did not win
  evidence: game 2 ply 168 as White: had a pawn one step from promotion but did not win
- repetition_draw (15 evidence): the learner drew by repetition
  evidence: game 1 ply 122 as Black: drew by repetition instead of changing the position
  evidence: game 4 ply 90 as White: drew by repetition instead of changing the position
- mate_loss (12 evidence): the learner lost by mate
  evidence: game 35 ply 37 as Black: lost by mate after 37 plies
  evidence: game 37 ply 59 as Black: lost by mate after 59 plies
