# Codex-chess-learner Memory

Codex-chess-learner starts with the same playing policy as Codex-chess. Future learning experiments should record durable observations here without changing the baseline engine.

Initial policy:
- Use only legal UCI moves from the supplied legal move list.
- Prefer sound development, king safety, and simple tactics.
- If uncertain, choose a practical legal move quickly rather than returning an invalid move.

Agent Skills:
- Engine-local skills may be placed under `skills/`.
- Skills should describe reusable chess-analysis or tournament-learning procedures for Codex to use when this engine runs.

Learning instruction:
- Use this `MEMORY.md` and create or update Agent Skills under `skills/` whenever a reusable chess improvement is discovered.
- Keep changes concise and focused on better move selection, opening choice, time management, illegal-move avoidance, and post-tournament learning.

Durable lessons:
- Standard Codex games can lose on clock before the chess position is lost. Manage time throughout the move, but still choose intentionally from the position; there is no fallback or client-picked move.
- A `0000` result after real plies means the engine forfeited after repeated invalid responses or timeouts. Treat that as a format/clock failure to avoid, not as a chess move.

<!-- learner-autolearn:start -->
## Autolearn Summary
- Last updated: 2026-05-12 10:30:20
- Current match score: 3.5 / 5 (70.0%).
- Result reasons: mate=4, threefold repetition=1.
- Apply `knowledgebase/live-match-lessons.md` before choosing moves.
- Avoid threefold repetition loops unless drawing is the only practical outcome.
- Manage the clock while still choosing a move intentionally; there is no fallback or client-picked move.
- Never return a move outside `legal_moves`; never return `0000` while legal moves exist.
<!-- learner-autolearn:end -->

<!-- fen-curriculum:start -->
## FEN Curriculum Summary
- Last updated: 2026-05-12 11:08:59
- Model: gpt-5.3-codex
- Final score: 50 / 50.
- Mastered held-out set: yes.
- Apply `knowledgebase/fen-curriculum-lessons.md` before interpreting any chess position.
- FEN piece placement is read rank 8 to rank 1, with files a through h inside each rank and digits as empty-square skips.
- Uppercase FEN letters are White pieces; lowercase letters are Black pieces.
- Always account for side-to-move, check status, castling rights, en-passant field, material counts, and legal-move constraints before choosing a move.
- Most recent weak concepts: legal move recognition.
<!-- fen-curriculum:end -->
