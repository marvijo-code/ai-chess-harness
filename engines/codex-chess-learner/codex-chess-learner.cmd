@echo off
setlocal
set "CODEX_CHESS_ENGINE_NAME=Codex-chess-learner"
set "CODEX_CHESS_AUTHOR=marvijo/Codex app-server learner"
set "CODEX_CHESS_ROOT=%~dp0..\.."
python "%~dp0..\codex-chess\codex_chess_uci.py"
