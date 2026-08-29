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

# Usage:
#   bash scripts/bench.sh                          # run all benchmarks
#   bash scripts/bench.sh tests/bench/test_audio_bench.py  # run specific file
#   bash scripts/bench.sh --benchmark-save=baseline        # save results
#   bash scripts/bench.sh --benchmark-compare=baseline     # compare to saved

if [[ $# -eq 0 ]]; then
  set -- tests/bench -m bench --benchmark-only
fi

exec "$VENV_PYTHON" -m pytest "$@"
