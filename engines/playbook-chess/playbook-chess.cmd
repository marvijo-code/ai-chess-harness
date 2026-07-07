@echo off
setlocal
rem Prefer PyPy (~2.5x NPS -> ~1 extra ply of search at the same movetime) when
rem present at C:\dev\pypy; fall back to CPython so the engine still runs on any
rem machine without PyPy installed. See MEMORY.md (PyPy runtime for depth-8 push).
if exist "C:\dev\pypy\pypy.exe" (
  "C:\dev\pypy\pypy.exe" "%~dp0playbook_chess_uci.py"
) else (
  python "%~dp0playbook_chess_uci.py"
)
