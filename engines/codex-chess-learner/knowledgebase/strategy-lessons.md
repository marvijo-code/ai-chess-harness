# Strategy Lessons

Generated: 2026-05-14 15:11:28
Completed games scanned: 12

This file stores model-discovered concepts from self-play evidence. It must not be treated as memorized move answers.

## Concept Synthesis
- status: unchanged
- message: no new self-play evidence

## Discovered Concepts
- high_value_checker_survival_gate; (confidence 0.99); trigger: a checking move with queen or rook lands on a square the enemy king can capture immediately and the check does not force mate or major gain; value adjustment: strongly penalize these checks; reward checks that keep the checking piece protected or outside king-capture range
- forcing_capture_requires_multi_recapture_screen; (confidence 0.96); trigger: a forcing capture places the moved piece on a square with multiple immediate enemy recaptures available; value adjustment: penalize forcing captures that fail a multi-recapture safety screen; reward preparatory moves that reduce recapture count or add defenders first
- promotion_race_over_nonessential_king_moves; (confidence 0.92); trigger: opponent has a near-promotion passer and own move options include non-forcing king repositioning; value adjustment: penalize king moves that ignore immediate promotion race defense; reward moves that block, capture, or force tempo against the passer
- convert_large_edge_by_piece_preservation; (confidence 0.95); trigger: side holds a large material advantage and can choose between safe conversion and tactical contact moves; value adjustment: increase value of low-risk consolidation, simplification, and passer support; decrease value of speculative tactics that expose major/minor pieces
- low_clock_simplification_and_decision_speed; (confidence 0.88); trigger: late-game state with many checking options but low remaining clock; value adjustment: penalize repetitive low-gain check sequences; reward faster simplifying plans that preserve material and reduce branching

## Evidence For Reflection
- material_swing (514 evidence): the opponent reply caused a material balance drop
  evidence: game 1 ply 16 as Black move c8e6: opponent reply Bxe6 shifted material by 300 centipawns
  evidence: game 1 ply 22 as Black move c5d4: opponent reply Nxd4 shifted material by 300 centipawns
- undefended_forcing_piece (216 evidence): a forcing move left the moved piece immediately capturable
  evidence: game 2 ply 15 as White move e4d5: forcing move leaves P capturable by d8d5, f6d5
  evidence: game 3 ply 20 as Black move e5d4: forcing move leaves p capturable by f3d4, d1d4, c3d4
- time_loss (72 evidence): the learner lost on time
  evidence: game 5 ply 31 as Black: lost on time after 31 plies
  evidence: game 6 ply 34 as White: lost on time after 34 plies
- hanging_checking_piece (59 evidence): a checking move left the moved piece capturable by the enemy king
  evidence: game 13 ply 22 as Black move h5h2: checking q can be captured by king move(s): g1h2
  evidence: game 14 ply 21 as White move d5f7: checking B can be captured by king move(s): e8f7
- failed_conversion (53 evidence): the learner had a material edge but did not win
  evidence: game 6 ply 34 as White: highest material edge was at least 900 centipawns but result was 0-1
  evidence: game 11 ply 21 as Black: highest material edge was at least 1100 centipawns but result was 1-0
- missed_king_capture (28 evidence): a legal king capture was available but another move was chosen
  evidence: game 7 ply 32 as Black move e8f7: legal king capture candidate(s) were available: g8f7
  evidence: game 11 ply 18 as Black move f8f7: legal king capture candidate(s) were available: g8f7
- pawn_promotion_failure (7 evidence): the learner had a pawn one step from promotion but did not win
  evidence: game 38 ply 36 as White: had a pawn one step from promotion but did not win
  evidence: game 2 ply 168 as White: had a pawn one step from promotion but did not win
- mate_loss (4 evidence): the learner lost by mate
  evidence: game 35 ply 37 as Black: lost by mate after 37 plies
  evidence: game 37 ply 59 as Black: lost by mate after 59 plies
- repetition_draw (3 evidence): the learner drew by repetition
  evidence: game 1 ply 122 as Black: drew by repetition instead of changing the position
  evidence: game 4 ply 90 as White: drew by repetition instead of changing the position
