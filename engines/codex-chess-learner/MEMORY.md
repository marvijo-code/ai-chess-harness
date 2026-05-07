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
