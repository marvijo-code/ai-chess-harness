# Tournament trophy counts

## Hide models without trophies

The top row now includes only models with at least one championship trophy.
Runner-up totals remain beside those models. Models with zero trophies are
hidden even when they have a runner-up finish. Full historical counts remain
in the data, so a model appears automatically after its first championship.

Verified on the live stream browser: exactly Stockfish 19, GPT-6.1 Sol, and
Sonnet 5.5 appeared, with positive trophies and the correct runner-up totals.
Icons were present, auto-focus stayed on, and no page or stream overflow was
found. All services remained active. Python compilation and diff checks passed.

Source SHA-256: 65ef5a4b2ff95bf4fa0f7175d14bf46aa46a82aafeaa92a97d7ffc901c0079a2.
Backup: /home/marvijo/ai-chess-trophies-20261013/viewer-before-hide-zero.py.
Evidence: C:/temp/ai-chess-trophies-20261013/trophy-winners-only-live.png.

## Runner-up extension

The deployed top row also shows a silver second-place medal and each model's
recorded runner-up total. Independently counted from the raw saved tournament
files: GPT-6.1 Sol 21, Sonnet 5.5 13, Stockfish 19 2, GPT-6 Astra 1; all others 0.
Only completed tournaments count. Missing runner-up fields are not inferred.

The extension passed 100 focused tests and browser checks at stream size and
widths 700, 375, and 320. Deployment preserved the runner and stream services.
Source SHA-256: 4fea2038acacacacaf43d623a984af73e7e834fa30fa31009035fda9904f22dd.
Backup: /home/marvijo/ai-chess-trophies-20261013/viewer-before-runner-ups.py.
Evidence: C:/temp/ai-chess-trophies-20261013/runner-ups-live.png.

The livestream now shows a top row with maker icons, names, gold trophy icons,
and counts of completed tournament championships. Each tournament id counts
once. Players with no titles show zero. One saved finished entry has no champion
and contributes no trophy. Historical models remain visible.

Verified totals: Stockfish 19 34, GPT-6.1 Sol 2, Sonnet 5.5 1, DeepSeek V4.1 Flash 0,
Fable 5.1 0, GLM 5.3 Flash 0, GPT-6 Astra 0. Counts update from saved history with
the viewer's existing 20 second history cache.

Validation: 98 focused tests passed. A separate viewer used the real live state
and history. Browser checks at widths 1920, 700, 375, and 320 found maker/trophy
icons, no overlapping trophy entries, and no page horizontal overflow. The live
stream screen measured 1919x1079; all game and side cards ended at or above
1044.75px. Auto-focus remained on. YouTube confirmed live/public and healthy.

Deployment updated only tools/llm_tournament_viewer.py on the VPS after checking
the original file hash and saving a backup. The viewer restarted and the stream
browser reloaded. The runner and video sender remained active.

Live source SHA-256: a9c227f3160829040d12755ec8fbca7ea722ca1eeba5a1ffaed5c72617f636a5.
Backup: /home/marvijo/ai-chess-trophies-20261013/viewer-before.py.
Local evidence: C:/temp/ai-chess-trophies-20261013/before-after.html.

No commit, push, or pull request was made. The exported public website page is
prepared at C:/temp/ai-chess-trophies-20261013/public-index.html but was not
published; the requested YouTube livestream display is deployed.
