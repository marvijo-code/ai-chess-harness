# Strategy Lessons

Generated: 2026-05-12 15:06:24
Completed games scanned: 0

This file stores model-discovered concepts from self-play evidence. It must not be treated as memorized move answers.

## Concept Synthesis
- status: unchanged
- message: no new self-play evidence

## Discovered Concepts
- Forcing-move recapture liability check; (confidence 0.88); trigger: Before playing a forcing capture or pawn break, the moved piece/pawn would be immediately capturable by multiple opponent units with no compensating gain.; value adjustment: Penalize the forcing move unless it wins material or improves king safety; reward alternatives that keep the capturing unit defended or make recapture less favorable.
- Single-jump exchange risk on advanced minors; (confidence 0.9); trigger: A minor piece jumps into enemy structure where a pawn recapture can remove it cleanly and open no clear tactical return.; value adjustment: Penalize such jumps when follow-up pressure is low; reward preserving the minor piece or choosing exchanges that do not concede immediate pawn recapture.
- Early time-triage under tactical contact; (confidence 0.91); trigger: Clock is already stressed while position has active captures/forcing replies available.; value adjustment: Increase value of fast, safe, low-branch decisions; reduce value of speculative tactical continuations that require long calculation without immediate material certainty.

## Evidence For Reflection
- material_swing (34 evidence): the opponent reply caused a material balance drop
  evidence: game 1 ply 16 as Black move f6e4: opponent reply Bxe4 shifted material by 300 centipawns
  evidence: game 1 ply 22 as Black move f6g5: opponent reply Nxg5+ shifted material by 300 centipawns
- undefended_forcing_piece (17 evidence): a forcing move left the moved piece immediately capturable
  evidence: game 1 ply 16 as Black move f6e4: forcing move leaves n capturable by d3e4
  evidence: game 1 ply 34 as Black move e6a2: forcing move leaves b capturable by a1a2
- hanging_checking_piece (7 evidence): a checking move left the moved piece capturable by the enemy king
  evidence: game 1 ply 78 as Black move e2e6: checking r can be captured by king move(s): f6e6
  evidence: game 1 ply 106 as Black move d4d6: checking q can be captured by king move(s): e6d6
- failed_conversion (6 evidence): the learner had a material edge but did not win
  evidence: game 1 ply 107 as Black: highest material edge was at least 900 centipawns but result was 1/2-1/2
  evidence: game 2 ply 40 as White: highest material edge was at least 1000 centipawns but result was 0-1
- time_loss (5 evidence): the learner lost on time
  evidence: game 2 ply 40 as White: lost on time after 40 plies
  evidence: game 5 ply 51 as Black: lost on time after 51 plies
- missed_king_capture (2 evidence): a legal king capture was available but another move was chosen
  evidence: game 1 ply 28 as Black move f7g6: legal king capture candidate(s) were available: h7g6
  evidence: game 3 ply 32 as Black move f8g8: legal king capture candidate(s) were available: h8g8
- pawn_promotion_failure (1 evidence): the learner had a pawn one step from promotion but did not win
  evidence: game 1 ply 107 as Black: had a pawn one step from promotion but did not win
