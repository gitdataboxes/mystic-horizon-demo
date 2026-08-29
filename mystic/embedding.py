"""Local ONNX embedding support."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast
from urllib import request as urllib_request

from mystic.config import (
    DEFAULT_LOCAL_EMBEDDING_MODEL,
    DEFAULT_LOCAL_EMBEDDING_DIMENSIONS,
    LocalEmbeddingConfig,
    get_error_message,
    get_providers_config,
    get_shared_home,
    logger,
)
from mystic.http import RequestTransport

EMBEDDING_HF_REVISION = "main"  # pin to specific commit hash for reproducibility

_onnx_session: Any | None = None
_tokenizer: Any | None = None
_session_model_dir: Path | None = None
_model_lock = threading.Lock()


def get_models_dir() -> Path:
    """Return shared model directory across agents."""
    return get_shared_home() / "models"


def embedding_model_missing(model_name: str | None = None) -> list[str]:
    """Return list of missing embedding model files (empty if all present)."""
    name = model_name or DEFAULT_LOCAL_EMBEDDING_MODEL
    model_dir = get_models_dir() / name
    return [f for f in ("model.onnx", "tokenizer.json") if not (model_dir / f).exists()]


def get_local_model_dir(model_name: str) -> Path:
    """Return model dir if files exist, otherwise raise."""
    model_dir = get_models_dir() / model_name
    missing = [f for f in ("model.onnx", "tokenizer.json") if not (model_dir / f).exists()]
    if missing:
        raise RuntimeError(
            f"Embedding model {model_name} not found (missing: {', '.join(missing)}). "
            "Run 'mystic-horizon --agent <name> init' to download it."
        )
    return model_dir


def ensure_local_model(
    model_name: str,
    progress_callback_factory: Callable[[str], Callable[[int, int | None], None] | None] | None = None,
) -> Path:
    model_dir = get_models_dir() / model_name
    model_path = model_dir / "model.onnx"
    tokenizer_path = model_dir / "tokenizer.json"
    if model_path.exists() and tokenizer_path.exists():
        return model_dir

    model_dir.mkdir(parents=True, exist_ok=True)
    hf_base = f"https://huggingface.co/nomic-ai/{model_name}/resolve/{EMBEDDING_HF_REVISION}"
    file_candidates: dict[str, tuple[str, ...]] = {
        "model.onnx": (
            f"{hf_base}/model.onnx",
            f"{hf_base}/onnx/model.onnx",
        ),
        "tokenizer.json": (
            f"{hf_base}/tokenizer.json",
        ),
    }

    for filename, candidates in file_candidates.items():
        destination = model_dir / filename
        if destination.exists():
            continue
        progress_callback = progress_callback_factory(filename) if progress_callback_factory else None
        _download_with_fallback(
            candidates,
            destination,
            model_name=model_name,
            filename=filename,
            progress_callback=progress_callback,
        )
    return model_dir


def _download_with_fallback(
    urls: Sequence[str],
    destination: Path,
    *,
    model_name: str,
    filename: str,
    progress_callback: Callable[[int, int | None], None] | None = None,
) -> None:
    last_error: Exception | None = None
    for url in urls:
        try:
            logger.info("embedding.model.downloading", model=model_name, file=filename, url=url)
            _download_file(url, destination, progress_callback=progress_callback)
            logger.info("embedding.model.downloaded", model=model_name, file=filename, path=str(destination))
            return
        except Exception as exc:  # pragma: no cover - network and filesystem dependent.
            last_error = exc
    detail = get_error_message(last_error) if last_error else "unknown error"
    raise RuntimeError(f"Failed to download {filename} for {model_name}: {detail}")


def _download_file(
    url: str,
    destination: Path,
    progress_callback: Callable[[int, int | None], None] | None = None,
) -> None:
    temp_path: Path | None = None
    try:
        with urllib_request.urlopen(url, timeout=120) as response:
            status = getattr(response, "status", 200)
            if status < 200 or status >= 300:
                raise RuntimeError(f"HTTP {status}")
            total = None
            content_length = response.headers.get("Content-Length")
            if content_length and content_length.isdigit():
                total = int(content_length)
            with tempfile.NamedTemporaryFile(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                downloaded = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback is not None:
                        progress_callback(downloaded, total)
        assert temp_path is not None
        temp_path.replace(destination)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def _get_session(model_dir: Path) -> tuple[Any, Any]:
    global _onnx_session, _tokenizer, _session_model_dir
    with _model_lock:
        if _onnx_session is not None and _tokenizer is not None and _session_model_dir == model_dir:
            return _onnx_session, _tokenizer

        try:
            import onnxruntime as ort  # type: ignore[import-not-found]
            from tokenizers import Tokenizer  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Local embeddings require onnxruntime and tokenizers. "
                "Install with: pip install onnxruntime tokenizers"
            ) from exc

        tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        session = ort.InferenceSession(str(model_dir / "model.onnx"), providers=["CPUExecutionProvider"])
        _onnx_session = session
        _tokenizer = tokenizer
        _session_model_dir = model_dir
        return session, tokenizer


def _embed_batch_sync(texts: list[str], prefix: str, model_dir: Path, dimensions: int) -> list[list[float]]:
    if not texts:
        return []

    if dimensions <= 0:
        raise ValueError("embedding dimensions must be positive")

    session, tokenizer = _get_session(model_dir)
    prefixed = [f"{prefix}{text}" for text in texts]
    encodings = tokenizer.encode_batch(prefixed)
    if not encodings:
        return []

    try:
        import numpy as np
    except ModuleNotFoundError as exc:  # pragma: no cover - numpy is an onnxruntime dependency.
        raise RuntimeError("numpy is required for local embedding inference") from exc

    max_length = max(len(encoding.ids) for encoding in encodings)
    input_ids = np.zeros((len(encodings), max_length), dtype=np.int64)
    attention_mask = np.zeros((len(encodings), max_length), dtype=np.int64)
    for index, encoding in enumerate(encodings):
        ids = np.asarray(encoding.ids, dtype=np.int64)
        length = ids.shape[0]
        input_ids[index, :length] = ids
        attention_mask[index, :length] = 1

    feeds: dict[str, Any] = {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
    }
    input_names = {str(item.name) for item in session.get_inputs()}
    if "token_type_ids" in input_names:
        feeds["token_type_ids"] = np.zeros_like(input_ids, dtype=np.int64)

    outputs = session.run(None, feeds)
    if not outputs:
        raise RuntimeError("Local embedding model returned no outputs")

    output = np.asarray(outputs[0], dtype=np.float32)
    if output.ndim == 3:
        mask = attention_mask.astype(np.float32)[..., None]
        pooled = (output * mask).sum(axis=1) / np.clip(mask.sum(axis=1), a_min=1.0, a_max=None)
    elif output.ndim == 2:
        pooled = output
    else:
        raise RuntimeError(f"Unexpected embedding output shape: {output.shape}")

    if dimensions > int(pooled.shape[1]):
        raise ValueError(
            f"embedding.dimensions ({dimensions}) exceeds model output width ({int(pooled.shape[1])})"
        )
    truncated = pooled[:, :dimensions]
    norms = np.linalg.norm(truncated, axis=1, keepdims=True)
    normalized = truncated / np.clip(norms, a_min=1e-12, a_max=None)
    return cast(list[list[float]], normalized.astype(float).tolist())


async def embed_chunks(
    chunks: Sequence[str],
    *,
    transport: RequestTransport | None = None,
) -> list[list[float]] | None:
    del transport
    if not chunks:
        return []

    embedding = get_providers_config().embedding
    try:
        model_dir = get_local_model_dir(embedding.model)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            _embed_batch_sync,
            list(chunks),
            "search_document: ",
            model_dir,
            embedding.dimensions,
        )
    except Exception as exc:
        logger.error("embedding.failed", error=get_error_message(exc))
        return None


async def embed_query(
    query: str,
    *,
    transport: RequestTransport | None = None,
) -> list[float] | None:
    del transport
    embedding = get_providers_config().embedding
    try:
        model_dir = get_local_model_dir(embedding.model)
        loop = asyncio.get_running_loop()
        vectors = await loop.run_in_executor(
            None,
            _embed_batch_sync,
            [query],
            "search_query: ",
            model_dir,
            embedding.dimensions,
        )
        return vectors[0] if vectors else None
    except Exception as exc:
        logger.error("embedding.query.failed", error=get_error_message(exc))
        return None


def default_local_embedding_config() -> LocalEmbeddingConfig:
    return LocalEmbeddingConfig(
        provider="local",
        model=DEFAULT_LOCAL_EMBEDDING_MODEL,
        dimensions=DEFAULT_LOCAL_EMBEDDING_DIMENSIONS,
    )
