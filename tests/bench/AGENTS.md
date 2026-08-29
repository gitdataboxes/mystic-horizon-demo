# tests/bench/ — Performance Benchmarks

## Purpose

`pytest-benchmark` suite for hot-path operations. All tests use the `@pytest.mark.bench` marker and are excluded from default `test-python.sh` runs.

## Files

- `conftest.py` — `bench_db` (empty schema) and `populated_db` (200 chunks, 5 people, 4 calls each) fixtures
- `test_audio_bench.py` — μ-law codec and resampling on 20ms frames (imports from `mystic.audio`)
- `test_chunking_bench.py` — `chunk_text` at short, medium, and long transcript sizes
- `test_db_bench.py` — `pack_embedding`, vec0 search, FTS single/multi-term, person-scoped queries
- `test_embedding_bench.py` — ONNX embedding inference (auto-skips if model not downloaded)
- `test_prompt_bench.py` — prompt rendering and variable computation benchmarks (uses day summaries plus verbatim recent-context variables)
- `test_llm_ttft_bench.py` — LLM time-to-first-token benchmark harness
- `test_retrieval_bench.py` — memory retrieval and hybrid search benchmarks
- `test_tts_bench.py` — TTS synthesis benchmarks

## Commands

- `bash scripts/bench.sh` — run all benchmarks
- `bash scripts/bench.sh tests/bench/test_audio_bench.py` — run a specific file
- `bash scripts/bench.sh --benchmark-save=baseline` — save results for comparison
- `bash scripts/bench.sh --benchmark-compare=baseline` — compare against saved results

## Conventions

- Every benchmark class and test uses `@pytest.mark.bench`
- Embedding benchmarks guard on model presence and skip cleanly
- `populated_db` fixture seeds realistic data (200 transcript chunks with embeddings)
