# Verification

Validated on Linux x86_64, CPython 3.11.15, with LiveKit Agents 1.5.1 on 2026-09-04:

- Non-benchmark suite: **630 passed, 3 skipped, 26 benchmark cases deselected**; three subtests also passed.
- The imported startup/streaming regressions are covered by `test_cli.py` and `test_voice_pipeline.py`.
- Every test receives a temporary application home by default, including tests that previously initialized migrations in the normal agent directory.

```bash
bash scripts/bootstrap-python.sh
bash scripts/test-python.sh
.venv/bin/python -m compileall -q mystic skills tests
```

The suite uses temporary databases/configuration and mocked provider behavior. Local server tests need loopback socket access. CI repeats syntax compilation and the non-benchmark suite on Ubuntu with Python 3.11.

Hardware speech, microphone capture, real telephone calls, LLM quality, and sustained-load behavior were not exercised in this run. Optional tests may skip when their additional runtime dependencies are absent. The existing benchmark suite is opt-in and is not a claim of measured performance.

Four aiohttp application-key warnings remain in this run; they do not fail the behavioral checks.
