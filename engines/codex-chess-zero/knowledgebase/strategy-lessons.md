# Strategy Lessons

Generated: 2026-05-18 17:02:05
Completed games scanned: 36

This file stores model-discovered concepts from self-play evidence. It must not be treated as memorized move answers.

## Concept Synthesis
- status: deferred
- message: concept synthesis deferred while live training is running

## Self Extension
- status: ok
- concept_count: 7
- skill: C:\dev\chess-harness-codex\engines\codex-chess-zero\skills\self-play-concepts\SKILL.md

## Discovered Concepts
- Do-not-sac-initiator-without-net-gain; (confidence 0.94); trigger: A forcing capture/check is made by a higher-value piece and that piece is immediately recapturable by equal or lower-value enemy piece with no follow-up material win; value adjustment: Strongly decrease move value; apply larger penalty when the initiating piece value is >= bishop/rook
- Penalty-for-hanging-checking-piece; (confidence 0.9); trigger: A checking move places the checking piece on a square capturable by the king or simple recapture, without mating net or major gain; value adjustment: Decrease move value; increase penalty if the checking piece is bishop/rook and capture is by king with low tactical risk
- Recapture-risk-filter-on-forcing-moves; (confidence 0.93); trigger: Candidate move is forcing (capture/check/threat) but lands on a square attacked by multiple enemy units and defended weakly; value adjustment: Apply pre-move safety tax proportional to (enemy_attackers - friendly_defenders) and attacker piece value
- Preserve-large-advantage-by-trade-safety; (confidence 0.82); trigger: Side has large material edge and can choose between sharp forcing line vs simplification/secure defense; value adjustment: Increase value of safe exchanges and king-safety consolidation; decrease speculative tactics that reopen counterplay
- King-capture-alert-priority; (confidence 0.76); trigger: After opponent sacrifice/entry, legal king capture exists that wins material and does not expose immediate tactical collapse; value adjustment: Increase priority of evaluating legal king recaptures before quieter alternatives
- Post-castle-tactical-blunder-check; (confidence 0.74); trigger: King-safety move (e.g., castling) is considered while loose material/tactical captures are available to opponent; value adjustment: Before rewarding king safety, subtract value if opponent has immediate high-confidence material win next move
- Low-clock-decision-simplification; (confidence 0.61); trigger: Clock pressure with multiple tactical candidates of similar static score; value adjustment: Prefer low-branching, materially safe continuations; reduce value of complex forcing sacs requiring long verification

## Evidence For Reflection
- material_swing (531 evidence): the opponent reply caused a material balance drop
  evidence: game 1 ply 26 as Black move c7e5: opponent reply Rxe5 shifted material by 300 centipawns
  evidence: game 1 ply 36 as Black move c6d4: opponent reply Nxd4 shifted material by 300 centipawns
- undefended_forcing_piece (452 evidence): a forcing move left the moved piece immediately capturable
  evidence: game 1 ply 26 as Black move c7e5: forcing move leaves b capturable by f3e5, e1e5
  evidence: game 2 ply 19 as White move g5e6: forcing move leaves N capturable by f7e6
- failed_conversion (92 evidence): the learner had a material edge but did not win
  evidence: game 2 ply 89 as White: highest material edge was at least 3100 centipawns but result was 1/2-1/2
  evidence: game 4 ply 60 as White: highest material edge was at least 2000 centipawns but result was 1/2-1/2
- missed_king_capture (35 evidence): a legal king capture was available but another move was chosen
  evidence: game 7 ply 36 as Black move f8g8: legal king capture candidate(s) were available: h8g8
  evidence: game 10 ply 45 as White move f1f2: legal king capture candidate(s) were available: g1f2
- hanging_checking_piece (28 evidence): a checking move left the moved piece capturable by the enemy king
  evidence: game 2 ply 47 as White move c1h6: checking B can be captured by king move(s): g7h6
  evidence: game 21 ply 26 as Black move b6f2: checking b can be captured by king move(s): g1f2
- repetition_draw (8 evidence): the learner drew by repetition
  evidence: game 1 ply 67 as White: drew by repetition instead of changing the position
  evidence: game 2 ply 130 as Black: drew by repetition instead of changing the position
- pawn_promotion_failure (6 evidence): the learner had a pawn one step from promotion but did not win
  evidence: game 25 ply 102 as White: had a pawn one step from promotion but did not win
  evidence: game 25 ply 102 as White: had a pawn one step from promotion but did not win
- mate_loss (1 evidence): the learner lost by mate
  evidence: game 17 ply 23 as Black: lost by mate after 23 plies
