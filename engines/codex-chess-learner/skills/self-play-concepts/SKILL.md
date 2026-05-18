---
name: self-play-concepts
description: Use engine-local self-play concepts as a chess candidate-move checklist without memorized move answers.
---

# Self-Play Concepts

Generated: 2026-05-18 11:27:58
Engine: Codex-chess-learner

## Rules

- Use these concepts as candidate-move evaluation features, not as an opening book.
- Do not map exact FENs, move numbers, or game IDs to preferred moves.
- Do not use Stockfish, Lc0, Maia, tablebases, opening books, or human-game imitation as move labels.
- For each candidate, check legal move, own king safety, immediate material swing, opponent best reply, and plan continuity.
- Prefer concepts that generalize across positions: loose pieces, failed forcing moves, conversion, promotion, time pressure, and repetition.

## Concepts

- recapture_only_if_structure_does_not_uncover_immediate_piece_loss (confidence 0.69); trigger: a center recapture/capture with a pawn or minor piece changes protection lines for own developed pieces that are already candidate targets; adjustment: penalize the recapture if it removes or weakens defenders and allows an immediate favorable opponent pickup/exchange; reward alternatives that keep the target piece defended before recapturing; why: a natural center capture was followed by an immediate opponent capture that improved their material outcome, indicating defender-structure damage after the recapture
- forcing_minor_capture_needs_global_loose_piece_scan (confidence 0.9); trigger: a forcing knight capture is considered and either the moved knight or another nearby developed piece becomes attackable in one move; adjustment: strongly penalize the forcing capture unless both the moved piece and other exposed pieces remain tactically covered after the opponent's best immediate reply; reward consolidating moves that remove loose-piece vulnerabilities first; why: the forcing knight capture created immediate tactical liability and was followed by a material loss sequence, showing the tactic failed due to post-move loose pieces rather than lack of forcing intent
