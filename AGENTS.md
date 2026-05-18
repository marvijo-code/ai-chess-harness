# Repo Codex Rules

- For non-trivial chess-harness implementation work, update `PRD.md` and `PRD_CHECKLIST.md` before changing code, keep checklist status current while working, and mark the relevant items complete only after focused validation.
- For viewer work, preserve the local account-free workflow, persisted UI preferences in `localStorage`, FastChess live mirror compatibility, fixed monotonic live-board behavior during concurrent `-Concurrency` runs, clickable in-progress match switching, collapsed-by-default Engine Config, and the no-fallback-move engine boundary.
- Keep `Codex-chess-zero` as a separate fast first-principles learner: select it through `-LearningEngine zero`, keep its memory and knowledgebase under `engines\codex-chess-zero`, and pass autolearn an explicit engine name/context so it does not write learner state.
- For learner/Zero speed work, prefer capped prompt payloads, lower configured per-turn learner/critical/Zero effort, and deferred live-watch concept synthesis before increasing FastChess `-Concurrency` on CPU-bound machines.
