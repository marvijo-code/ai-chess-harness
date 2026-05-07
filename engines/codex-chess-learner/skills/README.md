# Codex-chess-learner Skills

Place learner-specific Agent Skills in this folder. Each skill should live in its own subfolder with a `SKILL.md` entrypoint.

The engine prompt points Codex at this folder as durable local skill context for learner runs.

The learner engine starts with `UseMemory=true`, `UseSkills=true`, and `LearningMode=true`. Future learner-specific skills should be added here as normal Agent Skill folders containing `SKILL.md`.
