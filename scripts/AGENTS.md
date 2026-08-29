# scripts/ — Developer Helpers

## Purpose

Small Python-oriented repo helpers for environment setup and test execution.

## Files

- `bootstrap-python.sh` — creates `.venv` and installs `mystic-horizon[dev]`
- `test-python.sh` — runs pytest through `.venv`, bootstrapping first if needed. Excludes benchmarks by default (`-m "not bench"`).
- `bench.sh` — runs performance benchmarks via `pytest-benchmark`. Supports `--benchmark-save=<name>` and `--benchmark-compare=<name>` for regression tracking.
- `typecheck.sh` — runs pyright in `standard` mode through the repo venv. Bootstraps if pyright is not installed.

## Conventions

- Prefer `.venv/bin/python` for repo-local commands.
- Keep scripts small and dependency-light.
