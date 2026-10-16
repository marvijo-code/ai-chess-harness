# One-hour conversations delivery proof

Verified on 16 October 2026, South African time.

## Delivered

- PR #57: one-hour clocks, disabled move cutoff, retained game reasoning, output ceilings, visible thinking summaries and background compaction.
- PR #58: a temporary roster hold that expires after a numbered tournament.
- Runtime commit: `a5ee2582c1d0c3e5759699dd6d0e3753106d5964` on the existing OVH server.
- Live tournament: #43, GPT-6.1 Sol, DeepSeek V4.1 Flash, GLM 5.3 Flash and Stockfish 19.
- Normal clock: 3,600,000 milliseconds per player plus 10,000 milliseconds per move.
- `moveCap: false`, `keepReasoning: true`; each AI is configured with high effort.
- Compactor: DeepSeek V4.1 Flash on OpenCode Go, high effort, at 120,000 input tokens.

The owner asked to remove Claude temporarily so play could resume. The operational hold file
contains `{"Sonnet 5.5": 43}`. The running configuration still enables Sonnet. Tournament #43
omits it from both probes and games. Tournament #44 includes it in the normal availability
check automatically, without another service restart. Availability still depends on its plan.

The old tournament #42 was blocked in semifinal `r6b2`, GPT versus Sonnet, after 39 plies.
Its full state was backed up before the owner-directed closure. The unfinished game's result
remains `*`; its status is `abandoned`, and no winner was invented. Completed results and
reflections were retained. Only the runner was restarted. The unrelated edited viewer file
kept SHA-256 `65ef5a4b2ff95bf4fa0f7175d14bf46aa46a82aafeaa92a97d7ffc901c0079a2`.

## Validation

- 156 focused tests passed; one POSIX-only binary-resolution test was skipped on Windows.
- The new conversation tests cover disabled cutoffs, full-clock waits, high-effort output-limit recovery, native sessions, carried HTTP reasoning, compaction and token ceilings.
- An integration test runs two consecutive tournaments: Sonnet gets no probe or game in the first and returns in the second. Invalid hold files fail explicitly.
- A fresh real eight-ply DeepSeek/GLM game completed all legal moves, showed thinking on all eight moves and applied three compactions.
- A fresh real eight-ply Codex/DeepSeek game completed all legal moves, showed thinking on seven moves and applied three compactions.
- The new local Sonnet/Codex test was blocked by Claude's session limit. It is not reported as a passing test. The owner then directed the first deployed tournament to omit Claude.
- All four live services were active. Both boards in tournament #43 advanced through legal moves.
- Live server settings match the merged source. Real thinking files contain provider-visible summaries or reasoning for Codex, DeepSeek and GLM.
- The public health endpoint identified tournament #43 with fresh state. A headless browser showed the real tournament and the 60-minute plus 10-second clock.
- YouTube reported the existing broadcast as public and live, with the stream active, health good and no issues. A frame from the actual stream showed tournament #43 and Sonnet's temporary hold.

This is a backend and runner change; the public viewer was not redesigned. Screenshots verify
the served state and the stream. No new win against Stockfish is claimed.

## Thinking visibility correction

PR #60 fixes a separate relay-cache defect found after deployment. Board IDs repeat across
tournaments. Old high move numbers occupied the relay's 40-move cache and caused the current
lower move number to be pruned immediately. The relay now persists a tournament association,
ignores unscoped legacy cache on startup and rejects explicitly stale tournament uploads.

All 56 relay and publisher tests passed before and after merge. Three new regressions failed
on the preceding code. The deployed relay file matched the merged source hash. Only the relay
and publisher restarted; the runner remained up from its earlier deployment, and the active
game kept its move history and clock.

The public thinking API then returned `exists: true`, size 483,483 and a 60,000-character tail.
A real browser displayed 60,045 characters in the open DeepSeek thinking panel. A screenshot
confirmed the visible text. The source file continued growing.

The model itself was also looping. In the latest 60,000 source characters, its Bxh7 line
appeared 1,200 times and its Qh5 line 1,199 times. No move was invented and its response was
left running under the owner's no-cutoff setting. A repetition guard was offered separately;
it was not enabled as part of the display repair.

## Links

- [Live tournament](https://marvijo.com/ai-chess)
- [YouTube livestream](https://www.youtube.com/watch?v=vnNTtpmCG_Y)
- [Conversation changes, PR #57](https://github.com/marvijo-code/ai-chess-harness/pull/57)
- [Automatic roster restoration, PR #58](https://github.com/marvijo-code/ai-chess-harness/pull/58)
