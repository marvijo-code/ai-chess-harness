# Zero First-Principles Climb Plan

Generated: 2026-05-19T20:34:37+02:00

## Boundary

Codex-chess-zero should improve from chess rules, legal moves, terminal outcomes, draw rules, and self-play only. It must not train on Stockfish, Lc0, Maia, tablebases, opening books, human games, or hand-authored chess motif penalties such as "knight on rim" or "rook shuffle bad".

External engines remain evaluation-only ladder opponents. Hand-authored tactical warnings may exist in a separate learner/deliberative engine, but not in Zero policy/value code.

## Research Takeaways

- AlphaZero's useful pattern is self-play reinforcement learning from random initialization with no domain knowledge except rules; its network learns policy and value from MCTS-guided self-play, not from human games or engine labels: https://arxiv.org/abs/1712.01815
- The policy target should be the MCTS root visit distribution, not only the selected move. The value target is the game outcome, with chess draws represented as `0`: https://arxiv.org/pdf/1712.01815
- Exploration is part of the algorithm, not a fallback: AlphaZero adds root prior noise for self-play and uses visit-count based move selection during training: https://arxiv.org/pdf/1712.01815
- Local compute cannot reproduce AlphaZero scale. The paper reports 700,000 training steps with 5,000 first-generation TPUs for self-play and 64 second-generation TPUs for training: https://arxiv.org/pdf/1712.01815
- Reanalysis is a practical sample-efficiency lever: rerun planning/search on existing data with the latest network to refresh improved policy/value targets without importing external labels: https://arxiv.org/abs/2104.06294
- KataGo shows that self-play systems can be accelerated by engineering the training loop and sampling, while still learning from neural-net-guided self-play: https://arxiv.org/abs/1902.10565
- Promotion gates should not depend on tiny noisy matches alone. Stockfish Fishtest uses statistical match testing concepts such as GSPRT; Zero can use a lightweight local version before trusting promotions: https://official-stockfish.github.io/docs/fishtest-wiki/Fishtest-Mathematics.html

## Plan

1. Stop bad data first.
   Add explicit `terminal_kind`, true-draw versus capped-draw separation, full state identity, and schema versioning before more sprinting. The current loop is sometimes adding useful replay evidence and sometimes reporting no new signal; better labels and state keys are the fastest way to stop poisoning the replay buffer.

2. Train policy from search, not just from the chosen move.
   Store root visit counts for legal moves from MCTS and train the policy head toward that distribution. The selected move alone throws away most of the improvement that search produced.

3. Make value draw-aware.
   Move from scalar win/loss pressure toward explicit WDL or at least separate win/draw/loss targets. Capped non-terminal games should be labelled separately and either use low value-loss weight or policy-only learning.

4. Add self-play exploration that is reproducible.
   Keep the seed-window fix, then add self-play-only root noise, early-ply temperature, randomized visit budgets, and deterministic seed logging. Evaluation and promotion must disable this noise.

5. Make replay diverse without using openings.
   Track first-8-ply self-play signatures and cap how many identical signatures can enter one training batch. This is data hygiene, not an opening book, because it only limits duplicate self-generated data.

6. Add replay reanalysis.
   Each loop round should refresh a small batch of stale self-play positions by rerunning MCTS with the current champion. Replace stale policy targets, but keep terminal outcomes unchanged.

7. Promote internally before asking Stockfish.
   New networks should first beat the current champion across a small diverse self-generated prefix suite and pass anti-regression metrics. Stockfish depth gates then become external reporting and ladder progression, not the only signal for whether learning improved.

8. Report the climb as a data pipeline.
   Every round should print WDL, true draw count, capped draw count, repetition draw count, unique opening signatures, replay added/updated/skipped, policy loss, value/WDL loss, internal gate score, and external ladder score.

## Short Implementation Checklist

- [x] Remove all hand-authored motif penalties from Zero feature weights/tests; keep only rule/state/self-play features. Target: under 1 hour.
- [x] Add `terminal_kind` and split true chess draws from capped draws in self-play records and climb summaries. Target: 1-2 hours.
- [x] Extend replay identity to include side, castling, en-passant, halfmove clock, and repetition bucket; add a schema version. Target: 1-2 hours.
- [x] Persist MCTS root visit counts and train policy against visit distributions for legal moves. Target: 1-2 hours.
- [x] Add self-play-only root noise plus early-ply temperature; disable both for evaluation/promotion. Target: 1-2 hours.
- [x] Add first-8-ply self-play signature metrics and cap duplicate signatures in training samples. Target: under 1 hour.
- [x] Add a small reanalysis command that refreshes policy targets for stale self-play positions with the current champion. Target: 1-2 hours.
- [x] Change promotion flow to champion-vs-candidate internal gate first, Stockfish depth gate second, with external games quarantined from replay. Target: 1-2 hours.
- [x] Add one climb metrics CSV/jsonl row per round with WDL/draw/repetition/diversity/replay/gate fields. Target: under 1 hour.

## First Patch Order

Start with checklist items 1-3. They do not require a neural-net redesign, they directly address the repeated-game/stale-signal symptoms, and they keep the no-external-label boundary intact.
