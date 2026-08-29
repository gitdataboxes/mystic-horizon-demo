#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${MH_VENV_DIR:-$ROOT_DIR/.venv}"
VENV_PYTHON="$VENV_DIR/bin/python"
PYRIGHT_BIN="$VENV_DIR/bin/pyright"

if [[ ! -x "$VENV_PYTHON" || ! -x "$PYRIGHT_BIN" ]]; then
  bash "$ROOT_DIR/scripts/bootstrap-python.sh"
fi

if ! "$PYRIGHT_BIN" --version >/dev/null 2>&1; then
  bash "$ROOT_DIR/scripts/bootstrap-python.sh"
fi

cd "$ROOT_DIR"
exec "$PYRIGHT_BIN" --pythonpath "$VENV_PYTHON" -p "$ROOT_DIR/pyproject.toml" "$@"
