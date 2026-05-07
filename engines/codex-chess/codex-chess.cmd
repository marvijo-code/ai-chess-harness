@echo off
setlocal
set "CODEX_CHESS_ROOT=%~dp0..\.."
python "%~dp0codex_chess_uci.py"
