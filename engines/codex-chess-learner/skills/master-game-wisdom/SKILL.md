---
name: master-game-wisdom
description: Learner-local Codex-authored chess principles synthesized from Lichess Elite master-game batches and Stockfish ladder attempts.
---

# Master Game Wisdom

Use this skill only for Codex-chess-learner prompt move selection.

Priority anti-blunder rules:
- Scan forcing replies before every move, especially checks, captures, mate threats, promotion threats, pins, forks, and attacks on loose valuable pieces.
- Reject moves that leave the king, queen, rook, passer, blockade, or key defender vulnerable to the opponent's best forcing reply.
- Do not grab material or automatically recapture unless the final position is safe.
- Resolve immediate checks, promotion threats, and loose-piece tactics before quiet improvement.
- Develop, contest the center, and secure the king before unsupported queen, rook, or flank activity.
- Move valuable pieces onto contested squares only with defenders, retreats, and no trap, pin, fork, or overload.
- Use checks only when they gain time, improve coordination, restrict the king, support conversion, or stop counterplay.
- In endgames, activate the king and pieces only while controlling checks, passers, blockades, and pawn races.

Rules:
- Use authored principles in the current position; do not treat this as an opening book.
- Do not use opening-family win rates, exact FEN-to-move rules, tablebase facts, Stockfish/Lc0/Maia PVs, or Zero training labels.
- If this skill conflicts with legal_moves, material_safety, king safety, or clock pressure, obey the current position.

Current principles:
- Scan forcing replies before every move, especially checks, captures, mate threats, promotion threats, pins, forks, and attacks on loose valuable pieces.
- Reject moves that leave the king, queen, rook, passer, blockade, or key defender vulnerable to the opponent's best forcing reply.
- Do not grab material or automatically recapture unless the final position is safe.
- Resolve immediate checks, promotion threats, and loose-piece tactics before quiet improvement.
- Develop, contest the center, and secure the king before unsupported queen, rook, or flank activity.
- Move valuable pieces onto contested squares only with defenders, retreats, and no trap, pin, fork, or overload.
- Use checks only when they gain time, improve coordination, restrict the king, support conversion, or stop counterplay.
- In endgames, activate the king and pieces only while controlling checks, passers, blockades, and pawn races.
- Convert passers with support, king safety, blockade control, and answers to checking counterplay.
- When calculation is limited, choose the stable move that preserves safety, coordination, and counterplay control.

Move-selection reminder:
- Choose only from legal_moves.
- Eliminate moves that hang the king, queen, rook, or a tactically loose piece.
- Then choose the move that best improves safety, activity, and conversion.

Detailed source: C:\dev\chess-harness-codex\engines\codex-chess-learner\knowledgebase\master-wisdom.md
