# Codex-chess-learner Memory

Codex-chess-learner starts with the same playing policy as Codex-chess. Future learning experiments should record durable observations here without changing the baseline engine.

Initial policy:
- Use only legal UCI moves from the supplied legal move list.
- Prefer sound development, king safety, and simple tactics.
- If uncertain, choose a practical legal move quickly rather than returning an invalid move.

Agent Skills:
- Engine-local skills may be placed under `skills/`.
- Skills should describe reusable chess-analysis or tournament-learning procedures for Codex to use when this engine runs.
