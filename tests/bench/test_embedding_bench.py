"""Benchmarks for ONNX embedding inference — the heaviest CPU work in the pipeline.

These benchmarks require the embedding model to be downloaded locally.
Skip automatically if the model files are not present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mystic.embedding import _embed_batch_sync, embedding_model_missing, get_local_model_dir
from tests.python_helpers import TempAppHome, seed_core_files, TEST_EMBEDDING_DIMENSIONS

_MODEL_NAME = "nomic-embed-text-v1.5"
_SKIP_REASON = f"embedding model {_MODEL_NAME} not downloaded (run 'mystic-horizon init')"


def _resolve_model_dir() -> Path | None:
    with TempAppHome() as home:
        seed_core_files(home)
        if embedding_model_missing(_MODEL_NAME):
            return None
        return get_local_model_dir(_MODEL_NAME)


# Resolve once at module load — avoids re-checking in every test
try:
    _MODEL_DIR: Path | None = _resolve_model_dir()
except Exception:
    _MODEL_DIR = None

_SHORT_TEXTS = ["The meeting is at 3pm on Tuesday."]
_MEDIUM_TEXTS = [
    "The quarterly report shows a 15% increase in engagement metrics.",
    "Infrastructure costs are projected to decrease after the cloud migration.",
    "The marketing team needs updated attribution data before the board meeting.",
]
_BATCH_TEXTS = [f"Sample document number {i} with some filler content for benchmarking." for i in range(16)]

_needs_model = pytest.mark.skipif(_MODEL_DIR is None, reason=_SKIP_REASON)


@pytest.mark.bench
@_needs_model
class TestEmbeddingInference:
    def test_single_text(self, benchmark):
        assert _MODEL_DIR is not None
        benchmark(_embed_batch_sync, _SHORT_TEXTS, "search_document: ", _MODEL_DIR, TEST_EMBEDDING_DIMENSIONS)

    def test_three_texts(self, benchmark):
        assert _MODEL_DIR is not None
        benchmark(_embed_batch_sync, _MEDIUM_TEXTS, "search_document: ", _MODEL_DIR, TEST_EMBEDDING_DIMENSIONS)

    def test_batch_16(self, benchmark):
        assert _MODEL_DIR is not None
        benchmark(_embed_batch_sync, _BATCH_TEXTS, "search_document: ", _MODEL_DIR, TEST_EMBEDDING_DIMENSIONS)

    def test_query_prefix(self, benchmark):
        assert _MODEL_DIR is not None
        benchmark(_embed_batch_sync, _SHORT_TEXTS, "search_query: ", _MODEL_DIR, TEST_EMBEDDING_DIMENSIONS)
