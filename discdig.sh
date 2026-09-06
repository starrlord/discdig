#!/usr/bin/env bash
# discdig launcher for POSIX shells / Git Bash.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1
if [ -x "$here/.venv/Scripts/python.exe" ]; then py="$here/.venv/Scripts/python.exe"
elif [ -x "$here/.venv/bin/python" ]; then py="$here/.venv/bin/python"
else py="python3"; fi
exec "$py" -m discdig "$@"
