# Codex-chess-zero Memory

Codex-chess-zero is a separate fast self-learning engine. It should use compact post-game feedback, but each move should be chosen from first principles using the current FEN, legal moves, clock, and material safety.

<!-- learner-autolearn:start -->
## Autolearn Summary
- Last updated: never
- Current match score: 0.0 / 0 (0.0%).
- Result reasons: none.
- Apply `knowledgebase/live-match-lessons.md` before choosing moves.
- Apply model-discovered concepts from `knowledgebase/strategy-lessons.md` as generic value adjustments, not as memorized move answers.
- Use current FEN, legal moves, clocks, and material safety as the source of truth.
- Play quickly; choose a practical legal move rather than spending the clock.
- Never return a move outside `legal_moves`; never return `0000` while legal moves exist.
<!-- learner-autolearn:end -->
