---
name: self-play-concepts
description: Use engine-local self-play concepts as a chess candidate-move checklist without memorized move answers.
---

# Self-Play Concepts

Generated: 2026-05-18 17:02:05
Engine: Codex-chess-zero

## Rules

- Use these concepts as candidate-move evaluation features, not as an opening book.
- Do not map exact FENs, move numbers, or game IDs to preferred moves.
- Do not use Stockfish, Lc0, Maia, tablebases, opening books, or human-game imitation as move labels.
- For each candidate, check legal move, own king safety, immediate material swing, opponent best reply, and plan continuity.
- Prefer concepts that generalize across positions: loose pieces, failed forcing moves, conversion, promotion, time pressure, and repetition.

## Concepts

- Do-not-sac-initiator-without-net-gain (confidence 0.94); trigger: A forcing capture/check is made by a higher-value piece and that piece is immediately recapturable by equal or lower-value enemy piece with no follow-up material win; adjustment: Strongly decrease move value; apply larger penalty when the initiating piece value is >= bishop/rook; why: Multiple forcing moves won tempo but immediately lost the attacker, producing repeated material swings against the mover
- Penalty-for-hanging-checking-piece (confidence 0.9); trigger: A checking move places the checking piece on a square capturable by the king or simple recapture, without mating net or major gain; adjustment: Decrease move value; increase penalty if the checking piece is bishop/rook and capture is by king with low tactical risk; why: Checks looked forcing but were neutralized by immediate king recapture, causing direct material loss
- Recapture-risk-filter-on-forcing-moves (confidence 0.93); trigger: Candidate move is forcing (capture/check/threat) but lands on a square attacked by multiple enemy units and defended weakly; adjustment: Apply pre-move safety tax proportional to (enemy_attackers - friendly_defenders) and attacker piece value; why: Repeated early forcing moves created immediate recapture opportunities and consistent ~300cp losses
- Preserve-large-advantage-by-trade-safety (confidence 0.82); trigger: Side has large material edge and can choose between sharp forcing line vs simplification/secure defense; adjustment: Increase value of safe exchanges and king-safety consolidation; decrease speculative tactics that reopen counterplay; why: A position with very large edge still ended drawn, indicating conversion failures from unnecessary complexity
- King-capture-alert-priority (confidence 0.76); trigger: After opponent sacrifice/entry, legal king capture exists that wins material and does not expose immediate tactical collapse; adjustment: Increase priority of evaluating legal king recaptures before quieter alternatives; why: Evidence shows missed or delayed king captures around forcing exchanges; fast recognition improves material retention
- Post-castle-tactical-blunder-check (confidence 0.74); trigger: King-safety move (e.g., castling) is considered while loose material/tactical captures are available to opponent; adjustment: Before rewarding king safety, subtract value if opponent has immediate high-confidence material win next move; why: A nominally good king-safety move was followed by immediate material loss, implying tactical scan must gate strategic bonuses
- Low-clock-decision-simplification (confidence 0.61); trigger: Clock pressure with multiple tactical candidates of similar static score; adjustment: Prefer low-branching, materially safe continuations; reduce value of complex forcing sacs requiring long verification; why: Pattern of forcing but unsafe captures suggests over-optimistic tactical choices; simplifying under time should reduce blunders
