@echo off
rem discdig launcher - runs the app out of its own virtualenv.
rem UTF-8 keeps the block-character progress bars and glyphs intact on Windows.
setlocal
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
"%~dp0.venv\Scripts\python.exe" -m discdig %*
