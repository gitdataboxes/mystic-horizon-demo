"""
PocketTTS ONNX - Pure ONNX inference for Pocket TTS

A standalone, production-ready class for text-to-speech with voice cloning.
Supports both offline (batch) and streaming modes with adaptive chunking.

Dependencies:
- onnxruntime (or onnxruntime-gpu for CUDA)
- numpy
- soundfile
- sentencepiece
- scipy (for resampling)

Usage:
from pocket_tts_onnx import PocketTTSOnnx

# Initialize with INT8 (CPU optimized - default, fastest)
tts = PocketTTSOnnx()

# Voice cloning from audio file
audio = tts.generate("Hello world!", voice="samples/reference.wav")

# Streaming with adaptive chunking
for chunk in tts.stream("Hello world!", voice="samples/reference.wav"):
    play_audio(chunk)
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Generator, Union

import numpy as np
import onnxruntime as ort
import sentencepiece as spm

try:
    import soundfile as sf

    HAS_SOUNDFILE = True
except ImportError:
    HAS_SOUNDFILE = False

try:
    import scipy.signal

    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False


class PocketTTSOnnx:
    """
    Pure ONNX inference engine for Pocket TTS.

    Supports:
    - Offline (batch) generation
    - Streaming generation with adaptive chunking
    - INT8 and FP32 models
    - Voice cloning from audio files
    - Auto GPU/CPU detection
    - Temperature control for generation diversity

    Args:
        models_dir: Directory containing ONNX models
        tokenizer_path: Path to sentencepiece tokenizer.model
        precision: Model precision - "int8" (CPU optimized, fastest) or "fp32"
        device: "auto", "cpu", or "cuda"
        temperature: Sampling temperature
        lsd_steps: Number of flow matching steps
    """

    SAMPLE_RATE = 24_000
    SAMPLES_PER_FRAME = 1_920
    FRAME_DURATION = SAMPLES_PER_FRAME / SAMPLE_RATE
    VALID_PRECISIONS = ("int8", "fp32")

    def __init__(
        self,
        models_dir: str = "onnx",
        tokenizer_path: str = "tokenizer.model",
        precision: str = "int8",
        device: str = "auto",
        temperature: float = 0.7,
        lsd_steps: int = 10,
    ) -> None:
        self.models_dir = Path(models_dir)
        if precision not in self.VALID_PRECISIONS:
            raise ValueError(f"precision must be one of {self.VALID_PRECISIONS}, got {precision!r}")
        self.precision = precision
        self.temperature = temperature
        self.lsd_steps = lsd_steps

        self.providers = self._get_providers(device)
        self.tokenizer = spm.SentencePieceProcessor()
        self.tokenizer.Load(str(tokenizer_path))
        self._load_models()
        self._precompute_flow_buffers()
        self._voice_cache: dict[str, np.ndarray] = {}

    def _get_providers(self, device: str) -> list[str]:
        if device == "cpu":
            return ["CPUExecutionProvider"]
        if device == "cuda":
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]

        available = ort.get_available_providers()
        if "CUDAExecutionProvider" in available:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        return ["CPUExecutionProvider"]

    def _load_models(self) -> None:
        suffix = "_int8" if self.precision == "int8" else ""
        flow_main_file = f"flow_lm_main{suffix}.onnx"
        flow_flow_file = f"flow_lm_flow{suffix}.onnx"
        mimi_file = f"mimi_decoder{suffix}.onnx"

        self.mimi_encoder = ort.InferenceSession(
            str(self.models_dir / "mimi_encoder.onnx"),
            providers=self.providers,
        )
        self.text_conditioner = ort.InferenceSession(
            str(self.models_dir / "text_conditioner.onnx"),
            providers=self.providers,
        )
        self.flow_lm_main = ort.InferenceSession(
            str(self.models_dir / flow_main_file),
            providers=self.providers,
        )
        self.flow_lm_flow = ort.InferenceSession(
            str(self.models_dir / flow_flow_file),
            providers=self.providers,
        )
        self.mimi_decoder = ort.InferenceSession(
            str(self.models_dir / mimi_file),
            providers=self.providers,
        )

    def _precompute_flow_buffers(self) -> None:
        dt = 1.0 / self.lsd_steps
        self._st_buffers: list[tuple[np.ndarray, np.ndarray]] = []
        for step in range(self.lsd_steps):
            start = step / self.lsd_steps
            end = start + dt
            self._st_buffers.append(
                (
                    np.array([[start]], dtype=np.float32),
                    np.array([[end]], dtype=np.float32),
                )
            )

    def _init_state(self, session: ort.InferenceSession) -> dict[str, np.ndarray]:
        state: dict[str, np.ndarray] = {}
        type_map = {
            "tensor(float)": np.float32,
            "tensor(int64)": np.int64,
            "tensor(bool)": np.bool_,
        }
        for input_meta in session.get_inputs():
            if not input_meta.name.startswith("state_"):
                continue
            shape = [size if isinstance(size, int) else 0 for size in input_meta.shape]
            dtype = type_map.get(input_meta.type, np.float32)
            state[input_meta.name] = np.zeros(shape, dtype=dtype)
        return state

    def _increment_step(self, state: dict[str, np.ndarray], count: int) -> None:
        for key in state:
            if "step" in key:
                state[key] = (state[key] + count).astype(np.int64)

    def _load_audio(self, path: Union[str, Path]) -> np.ndarray:
        if not HAS_SOUNDFILE:
            raise ImportError("soundfile required for voice cloning. Install with: pip install soundfile")

        audio, sample_rate = sf.read(str(path))
        if len(audio.shape) > 1:
            audio = audio.mean(axis=1)

        if sample_rate != self.SAMPLE_RATE:
            if not HAS_SCIPY:
                raise ImportError("scipy required for resampling. Install with: pip install scipy")
            sample_count = int(len(audio) * self.SAMPLE_RATE / sample_rate)
            audio = scipy.signal.resample(audio, sample_count)

        audio = audio.astype(np.float32)
        if np.abs(audio).max() > 1.0:
            audio = audio / np.abs(audio).max()
        return audio.reshape(1, 1, -1)

    def encode_voice(self, audio_path: Union[str, Path]) -> np.ndarray:
        audio = self._load_audio(audio_path)
        embeddings = self.mimi_encoder.run(None, {"audio": audio})[0]

        while embeddings.ndim > 3:
            embeddings = embeddings.squeeze(0)
        if embeddings.ndim < 3:
            embeddings = embeddings[None]
        return embeddings

    def _get_voice_embeddings(self, voice: Union[str, Path, np.ndarray]) -> np.ndarray:
        if isinstance(voice, np.ndarray):
            return voice

        voice_key = str(voice)
        cached = self._voice_cache.get(voice_key)
        if cached is not None:
            return cached

        if not os.path.exists(voice_key):
            raise ValueError(f"Voice file {voice_key!r} not found.")

        embeddings = self.encode_voice(voice_key)
        self._voice_cache[voice_key] = embeddings
        return embeddings

    def _tokenize(self, text: str) -> np.ndarray:
        text = text.strip()
        if not text:
            raise ValueError("Text cannot be empty")
        if text[-1].isalnum():
            text = text + "."
        if not text[0].isupper():
            text = text[0].upper() + text[1:]
        token_ids = self.tokenizer.Encode(text)
        return np.array(token_ids, dtype=np.int64).reshape(1, -1)

    def _update_state_from_outputs(
        self,
        state: dict[str, np.ndarray],
        result: list[np.ndarray],
        session: ort.InferenceSession,
    ) -> None:
        for index in range(2, len(session.get_outputs())):
            name = session.get_outputs()[index].name
            if not name.startswith("out_state_"):
                continue
            output_index = int(name.replace("out_state_", ""))
            state[f"state_{output_index}"] = result[index]

    def _run_flow_lm(
        self,
        voice_embeddings: np.ndarray,
        text_ids: np.ndarray,
        max_frames: int = 500,
        frames_after_eos: int = 3,
    ) -> Generator[np.ndarray, None, None]:
        text_emb = self.text_conditioner.run(None, {"token_ids": text_ids})[0]
        if text_emb.ndim == 2:
            text_emb = text_emb[None]

        state = self._init_state(self.flow_lm_main)
        empty_seq = np.zeros((1, 0, 32), dtype=np.float32)
        empty_text = np.zeros((1, 0, 1024), dtype=np.float32)

        res_voice = self.flow_lm_main.run(
            None,
            {
                "sequence": empty_seq,
                "text_embeddings": voice_embeddings,
                **state,
            },
        )
        self._update_state_from_outputs(state, res_voice, self.flow_lm_main)

        res_text = self.flow_lm_main.run(
            None,
            {
                "sequence": empty_seq,
                "text_embeddings": text_emb,
                **state,
            },
        )
        self._update_state_from_outputs(state, res_text, self.flow_lm_main)

        curr = np.full((1, 1, 32), np.nan, dtype=np.float32)
        dt = 1.0 / self.lsd_steps
        eos_step: int | None = None

        for step in range(max_frames):
            res_step = self.flow_lm_main.run(
                None,
                {
                    "sequence": curr,
                    "text_embeddings": empty_text,
                    **state,
                },
            )
            conditioning = res_step[0]
            eos_logit = res_step[1]
            self._update_state_from_outputs(state, res_step, self.flow_lm_main)

            if eos_logit[0][0] > -4.0 and eos_step is None:
                eos_step = step
            if eos_step is not None and step >= eos_step + frames_after_eos:
                break

            std = np.sqrt(self.temperature) if self.temperature > 0 else 0.0
            x = (
                np.random.normal(0, std, (1, 32)).astype(np.float32)
                if std > 0
                else np.zeros((1, 32), dtype=np.float32)
            )

            for start_arr, end_arr in self._st_buffers:
                flow_out = self.flow_lm_flow.run(
                    None,
                    {
                        "c": conditioning,
                        "s": start_arr,
                        "t": end_arr,
                        "x": x,
                    },
                )
                x = x + flow_out[0] * dt

            latent = x.reshape(1, 1, 32)
            yield latent
            curr = latent

    def _decode_latents(self, latents: np.ndarray, chunk_size: int = 15) -> np.ndarray:
        state = self._init_state(self.mimi_decoder)
        audio_chunks: list[np.ndarray] = []
        frame_count = latents.shape[1]

        for index in range(0, frame_count, chunk_size):
            chunk = latents[:, index:index + chunk_size, :]
            result = self.mimi_decoder.run(None, {"latent": chunk, **state})
            audio_chunks.append(result[0].squeeze())

            for output_index in range(1, len(self.mimi_decoder.get_outputs())):
                output_name = self.mimi_decoder.get_outputs()[output_index].name
                if not output_name.startswith("out_state_"):
                    continue
                state_index = int(output_name.replace("out_state_", ""))
                state[f"state_{state_index}"] = result[output_index]

        return np.concatenate(audio_chunks)

    def generate(
        self,
        text: str,
        voice: Union[str, Path, np.ndarray],
        max_frames: int = 500,
    ) -> np.ndarray:
        voice_emb = self._get_voice_embeddings(voice)
        text_ids = self._tokenize(text)
        latents = list(self._run_flow_lm(voice_emb, text_ids, max_frames))
        latents = np.concatenate(latents, axis=1)
        return self._decode_latents(latents)

    def stream(
        self,
        text: str,
        voice: Union[str, Path, np.ndarray],
        max_frames: int = 500,
        first_chunk_frames: int = 2,
        target_buffer_sec: float = 0.2,
        max_chunk_frames: int = 15,
    ) -> Generator[np.ndarray, None, None]:
        voice_emb = self._get_voice_embeddings(voice)
        text_ids = self._tokenize(text)

        mimi_state = self._init_state(self.mimi_decoder)
        generated_latents: list[np.ndarray] = []
        decoded_frames = 0
        playback_start_time: float | None = None
        start_time = time.time()

        for latent in self._run_flow_lm(voice_emb, text_ids, max_frames):
            generated_latents.append(latent)
            pending = len(generated_latents) - decoded_frames
            chunk_size = 0

            if playback_start_time is None:
                if pending >= first_chunk_frames:
                    chunk_size = first_chunk_frames
            else:
                elapsed = time.time() - start_time
                audio_decoded_sec = decoded_frames * self.FRAME_DURATION
                playback_elapsed = elapsed - playback_start_time
                buffer_sec = audio_decoded_sec - playback_elapsed
                if buffer_sec < target_buffer_sec and pending >= 1:
                    chunk_size = min(pending, 3)
                elif pending >= max_chunk_frames:
                    chunk_size = max_chunk_frames

            if chunk_size <= 0:
                continue

            latents_chunk = np.concatenate(
                generated_latents[decoded_frames:decoded_frames + chunk_size],
                axis=1,
            )
            result = self.mimi_decoder.run(None, {"latent": latents_chunk, **mimi_state})
            audio_chunk = result[0].squeeze()
            for index, value in enumerate(result[1:]):
                mimi_state[f"state_{index}"] = value
            decoded_frames += chunk_size

            if playback_start_time is None:
                playback_start_time = time.time() - start_time

            yield audio_chunk

        if decoded_frames < len(generated_latents):
            remaining_latents = np.concatenate(generated_latents[decoded_frames:], axis=1)
            result = self.mimi_decoder.run(None, {"latent": remaining_latents, **mimi_state})
            yield result[0].squeeze()

    def save_audio(self, audio: np.ndarray, path: Union[str, Path]) -> None:
        if not HAS_SOUNDFILE:
            raise ImportError("soundfile required. Install with: pip install soundfile")
        sf.write(str(path), audio, self.SAMPLE_RATE)

    @property
    def device(self) -> str:
        if "CUDAExecutionProvider" in self.providers:
            return "cuda"
        return "cpu"

    def __repr__(self) -> str:
        return (
            f"PocketTTSOnnx("
            f"device={self.device!r}, "
            f"precision={self.precision!r}, "
            f"temperature={self.temperature}, "
            f"lsd_steps={self.lsd_steps}, "
            f"sample_rate={self.SAMPLE_RATE})"
        )
