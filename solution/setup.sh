#!/usr/bin/env bash
set -euo pipefail

SOLUTION_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_COMMAND="${PYTHON_COMMAND:-python3}"
VENV_DIR="${VENV_DIR:-$SOLUTION_DIR/.venv}"

"$PYTHON_COMMAND" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$SOLUTION_DIR/requirements.txt"

echo "Environment ready: $VENV_DIR"
echo "Run the solution with: $SOLUTION_DIR/run.sh"
