# Codex-chess-zero Memory

Codex-chess-zero is a separate fast self-learning engine. It should use compact post-game feedback, but each move should be chosen from first principles using the current FEN, legal moves, clock, and material safety.

<!-- learner-autolearn:start -->
## Autolearn Summary
- Last updated: 2026-05-19 18:26:05
- Current match score: 26.0 / 36 (74.29%).
- Result reasons: illegal move=1, mate=20, normal=1, threefold repetition=14.
- Apply `knowledgebase/live-match-lessons.md` before choosing moves.
- Apply model-discovered concepts from `knowledgebase/strategy-lessons.md` as generic value adjustments, not as memorized move answers.
- Use engine-local `skills/self-play-concepts/SKILL.md` and `tools/self_play_concepts.json` only as generalized self-play concept aids, never as exact move memory.
- Avoid threefold repetition loops unless drawing is the only practical outcome.
- Manage the clock while still choosing a move intentionally; there is no fallback or client-picked move.
- Never return a move outside `legal_moves`; never return `0000` while legal moves exist.
<!-- learner-autolearn:end -->
