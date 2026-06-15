@echo off
setlocal
rem Hybrid search backend for diagnostics only — not used in the wisdom climb ladder.
set WISDOM_CHESS_SF_DEPTH=18
python "%~dp0wisdom_chess_uci.py"
