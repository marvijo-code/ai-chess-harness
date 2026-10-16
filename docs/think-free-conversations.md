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

The source thinking also contained repetition. In the latest 60,000 source characters, its Bxh7 line
appeared 1,200 times and its Qh5 line 1,199 times. No move was invented and its response was
left running under the owner's no-cutoff setting. A repetition guard was offered separately;
it was not enabled as part of the display repair.

## Repetition root-cause investigation

The original DeepSeek move received 384,000 output tokens according to the provider's usage
event. It took 1,379 seconds before the output ceiling was reached. The harness then returned
the model's own thoughts at high effort, and DeepSeek supplied `g6` in the follow-up. The
completed move used 1,385.6 seconds of its own clock. This was one generation plus recovery,
not a series of hidden illegal-move retries. The preceding move's thinking did not contain
the repeated Bxh7 phrase.

The historical raw network stream was not recorded, so the exact cause of that generation's
repetition is not proven. Reconstructing from saved moves, frozen memory and visible reasoning
recovers the same position and memory hash, but normalises earlier JSON replies. Its input
count is 22,530 versus the original 22,550. It must not be called a byte-identical replay.

Four direct Go subscription probes captured raw response events before the chess harness:

- Reconstructed conversation: completed normally in 48.3 seconds, no repeated-text pattern.
- Same conversation with the provider author's sampling settings: completed normally in 76.3 seconds, no repeated-text pattern.
- Conversation with carried reasoning removed: still reasoning at the 100-second diagnostic boundary, no repeated-text pattern. Completion remains unknown.
- Fresh conversation with the same real position, memory and move history: still reasoning at the 100-second diagnostic boundary, no repeated-text pattern. Completion remains unknown.

Every captured response was replayed offline through the real harness parser. Parsed reasoning
and the written thinking file matched the raw reasoning exactly in all four captures. These
tests do not reproduce the original failure and do not prove the model, gateway or prompt is
the cause. Lowering effort or adding an interruption policy would be a workaround, not a
confirmed root-cause fix.

Live move clients now keep a bounded raw-event tail in memory. If repeated text is observed,
they save `<thinking-file>.provider-wire.json` privately beside the source thinking file. The
evidence includes the exact bounded request, its hash, client session identifier, raw provider
events and final usage. No authentication headers are accepted or written. Raw response storage
is capped at 512 KiB and request storage at 1 MiB. Normal replies create no diagnostic file.
This observation does not stop generation, change settings, choose a move or change the clock
policy. The runner reuses engine processes across games, so pulling the source alone does not
upgrade an existing process. After DeepSeek's game ended in checkmate at 41 plies, its exact
idle engine process was retired. The runner starts an updated engine for its next game.
No active game was interrupted; the other engines, viewer and stream were preserved.

The exact trigger remains unconfirmed until a failing response is captured at the wire.

A separate real-client integration probe used DeepSeek high-effort conversations against
Stockfish depth 4. At the owner's request for a handover, it was stopped after 19 completed
plies while the twentieth turn was still receiving varied thinking. No repetition evidence
file had appeared. This was an incomplete diagnostic, not a completed game or a reproduction
of the historical opening. Its private processes were closed; live games were left running.

The local handover and evidence bundle are in `out/deepseek-root-cause-20261016/` in the
investigation worktree.

### Second review (2026-10-16): where the repeated text came from

The harness did not copy the text twice. The answer request after the loop sent the model
120,053 characters of its own thinking, and the provider counted 55,986 extra input tokens
for them: about 2.07 characters per token in the looping part. Normal DeepSeek thinking in
the same game measured about 2.65 characters per token (16,737 characters for 6,355
reasoning tokens at ply 2). At those rates the 805,569-character ply 18 file holds about
388,000 tokens. The provider billed 384,000 output tokens for that one request. A copy by
the harness or the gateway would have made the file about twice that size.

The loop is not tied to this week's request shape. All 36,863 stored thinking files from
seven players were scanned for an eight-word phrase repeated 20 or more times. Only two
files matched, and both were DeepSeek V4.1 Flash. The other was tournament #5 on
2026-10-08, ply 34: a different route (Alibaba), a move cap, no carried thinking, and a
short "answer now" request. GPT, Sonnet, GLM, Fable and Astra had none.

Missing temperature is not the cause. Six identical small requests on the Go route, three
without temperature and three at 1.0, gave six different answers and different reasoning.
The gateway default samples normally.

Conclusion: the repeated text was generated by DeepSeek V4.1 Flash on the provider side
and billed as output. It happens rarely and is not reproducible on demand. The harness did
not create it. With no move cap, the harness lets such a loop run until the 384,000-token
limit, which took 1,379 seconds of the player's clock.

### Harness bug found on the way: streams that kept running after a cut

The #5 file showed two streams mixed word by word. The engine log for that move says the cut
stream had 2,669 characters of thinking, but the file had 12,612 characters before its first
marker. Cause: when the harness stopped listening before the provider's headers arrived, the
request thread had no response to close yet. It later received the response and kept
streaming into the current thinking file, which can be a later move's file, and kept spending
tokens. A local streaming server reproduces it on Linux (`tests/test_http_stream_cut.py`).
The fix sets a stop flag when the harness stops listening. A late response is closed at once,
and no event is written after the stop. With today's no-cap settings, cuts are rare, so this
bug did not cause the #43 loop.

Sources for the diagnostic controls: the [model author's sampling guidance](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash)
and [DeepSeek's reasoning/history protocol](https://api-docs.deepseek.com/guides/thinking_mode/).

### Loop guard and BEST SO FAR referee (owner request, 2026-10-16)

What happens when a reasoning stream loops (HTTP routes: DeepSeek, GLM and their OpenRouter second routes):

1. While reasoning streams in, every 1,024 new characters the engine checks the newest 8,192. If one
   8-word phrase appears 20 or more times, the stream is stopped and the connection is closed, so the
   provider stops generating. No other stored thinking file of any model reached 20 even across its
   whole text; #43 ply 18 would have stopped within about 60 repeats instead of 384,000 tokens.
2. The thinking up to the first copy of the repeated phrase goes back to the model as its own turn,
   with "Your previous reply kept repeating the same line, so it was stopped." It is asked for only
   the JSON move at the same effort (at most 16,000 output tokens). The same guard watches this answer.
3. The repeats are not carried into the next turn: the carried thinking is the trimmed text.
4. If that answer has no legal move, the referee plays the model's latest legal `BEST SO FAR` move
   from its thinking before the repeats. With none, that try is rejected and the normal retries follow.

`BEST SO FAR` (rule 11 of the shared rules): every player is asked to write `BEST SO FAR: <move>` as
soon as it has a first choice and again whenever the choice changes. The instruction had been removed
from the prompt on 2026-10-06 (PR #5), so the extractor had nothing to read; ply 18 had no such line.
After the last allowed try, the referee now plays the latest legal BEST SO FAR move from the failed
tries' visible thinking instead of a forfeit. Notes from an earlier move are never used. With no note,
the forfeit rule is unchanged. `LLM_LOOP_GUARD=0` turns the guard off.

Haiku 5.5 joins the roster on the Claude subscription (`claude-haiku-5-5`, high effort, its own memory
folder `haiku`). Sonnet 5.5's hold ended with #43. A running tournament keeps its own roster, so both
play from #44.

## Links

- [Live tournament](https://marvijo.com/ai-chess)
- [YouTube livestream](https://www.youtube.com/watch?v=vnNTtpmCG_Y)
- [Conversation changes, PR #57](https://github.com/marvijo-code/ai-chess-harness/pull/57)
- [Automatic roster restoration, PR #58](https://github.com/marvijo-code/ai-chess-harness/pull/58)
