#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${MH_VENV_DIR:-$ROOT_DIR/.venv}"
VENV_PYTHON="$VENV_DIR/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
  bash "$ROOT_DIR/scripts/bootstrap-python.sh"
fi

if ! "$VENV_PYTHON" -m pytest --version >/dev/null 2>&1; then
  bash "$ROOT_DIR/scripts/bootstrap-python.sh"
fi

cd "$ROOT_DIR"

if [[ $# -eq 0 ]]; then
  set -- tests -m "not bench"
fi

exec "$VENV_PYTHON" -m pytest "$@"
