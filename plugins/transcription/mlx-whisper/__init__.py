"""MLX Whisper transcription backend with a local CPU fallback.

The optional inference packages are deliberately imported only when a
transcription is requested. This keeps plugin discovery cheap and lets the
provider report availability without initializing Metal, MLX, or CTranslate2.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import platform
import sys
import threading
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from agent.transcription_provider import TranscriptionProvider

logger = logging.getLogger(__name__)

PROVIDER_NAME = "mlx-whisper"
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo-q4"
FALLBACK_MODEL = "small"

_MLX_BACKEND = "mlx-whisper"
_FALLBACK_BACKEND = "faster-whisper"

# mlx_whisper.transcribe() owns its model cache. Keep the imported module
# around too, and use one lock for the complete load/transcribe operation:
# Metal/MLX and faster-whisper model instances are not safe to use
# concurrently from this process.
_TRANSCRIPTION_LOCK = threading.RLock()
_mlx_whisper_module: Any = None
_fallback_model: Any = None

_MODEL_CATALOG = (
    {
        "id": DEFAULT_MODEL,
        "display": "Whisper Large v3 Turbo Q4",
        "backend": _MLX_BACKEND,
        "quantization": "4-bit",
        "languages": ["multilingual"],
    },
    {
        "id": "mlx-community/whisper-large-v3-turbo",
        "display": "Whisper Large v3 Turbo",
        "backend": _MLX_BACKEND,
        "quantization": "full precision",
        "languages": ["multilingual"],
    },
    {
        "id": "mlx-community/whisper-large-v3-turbo-q5",
        "display": "Whisper Large v3 Turbo Q5",
        "backend": _MLX_BACKEND,
        "quantization": "5-bit",
        "languages": ["multilingual"],
    },
    {
        "id": "mlx-community/whisper-large-v3-turbo-q8",
        "display": "Whisper Large v3 Turbo Q8",
        "backend": _MLX_BACKEND,
        "quantization": "8-bit",
        "languages": ["multilingual"],
    },
)


def _module_is_available(module_name: str) -> bool:
    """Return whether an optional module can be resolved without importing it."""
    if module_name in sys.modules:
        return sys.modules[module_name] is not None
    try:
        return importlib.util.find_spec(module_name) is not None
    except Exception:  # noqa: BLE001 - availability probes must not raise
        return False


def _mlx_supported_host() -> bool:
    """MLX needs macOS and a native Apple Silicon Python interpreter."""
    return sys.platform == "darwin" and platform.machine().lower() in {
        "arm64",
        "aarch64",
    }


def _optional_text(value: Any) -> Optional[str]:
    """Normalize an optional text argument while ignoring unsupported values."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _transcription_text(result: Any) -> str:
    """Extract text from the result shapes used by MLX Whisper."""
    if isinstance(result, Mapping):
        text = result.get("text")
        if text is None:
            segments = result.get("segments")
            if segments is not None:
                return _join_segment_text(segments)
    elif isinstance(result, str):
        text = result
    else:
        text = getattr(result, "text", None)

    if text is None:
        raise ValueError("MLX Whisper returned no transcript text")
    return str(text).strip()


def _join_segment_text(segments: Any) -> str:
    """Join segment text without depending on a backend-specific segment type."""
    texts: List[str] = []
    for segment in segments:
        if isinstance(segment, Mapping):
            text = segment.get("text", "")
        else:
            text = getattr(segment, "text", "")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return " ".join(texts).strip()


def _success_envelope(transcript: str, backend: str) -> Dict[str, Any]:
    return {
        "success": True,
        "transcript": transcript,
        "provider": PROVIDER_NAME,
        "backend": backend,
    }


def _error_envelope(error: str, backend: str) -> Dict[str, Any]:
    return {
        "success": False,
        "transcript": "",
        "error": error,
        "provider": PROVIDER_NAME,
        "backend": backend,
    }


def _load_mlx_whisper() -> Any:
    """Install when allowed and import MLX Whisper on the first use."""
    global _mlx_whisper_module
    if not _mlx_supported_host():
        raise RuntimeError("MLX Whisper is unavailable on this architecture")
    if _mlx_whisper_module is None:
        if not _module_is_available("mlx_whisper"):
            from tools.lazy_deps import ensure

            # Never prompt from a gateway/voice turn. The global lazy-install
            # policy remains the explicit opt-in/opt-out boundary.
            ensure("stt.mlx_whisper", prompt=False)
            importlib.invalidate_caches()
        _mlx_whisper_module = importlib.import_module("mlx_whisper")
    return _mlx_whisper_module


def _load_fallback_model() -> Any:
    """Load one cached fallback through Hermes's crash-safe native loader."""
    global _fallback_model
    if _fallback_model is None:
        if not _module_is_available("faster_whisper"):
            from tools.lazy_deps import ensure

            ensure("stt.faster_whisper", prompt=False)
            importlib.invalidate_caches()
        from tools.transcription_tools import _load_local_whisper_model

        _fallback_model = _load_local_whisper_model(
            FALLBACK_MODEL,
            device="cpu",
            compute_type="int8",
        )
    return _fallback_model


def _transcribe_fallback(
    file_path: str,
    *,
    language: Optional[str],
    initial_prompt: Optional[str],
    primary_error: BaseException,
) -> Dict[str, Any]:
    """Transcribe through the cached, serialized faster-whisper fallback."""
    try:
        model = _load_fallback_model()
        transcribe_kwargs: Dict[str, Any] = {
            "beam_size": 1,
            "vad_filter": True,
        }
        if language:
            transcribe_kwargs["language"] = language
        if initial_prompt:
            transcribe_kwargs["initial_prompt"] = initial_prompt

        segments, _info = model.transcribe(file_path, **transcribe_kwargs)
        transcript = _join_segment_text(segments)
        logger.info(
            "Transcription succeeded (provider=%s, backend=%s)",
            PROVIDER_NAME,
            _FALLBACK_BACKEND,
        )
        return _success_envelope(transcript, _FALLBACK_BACKEND)
    except Exception as exc:  # noqa: BLE001 - provider boundary must not raise
        logger.warning(
            "Transcription backend failed (provider=%s, backend=%s, error=%s)",
            PROVIDER_NAME,
            _FALLBACK_BACKEND,
            type(exc).__name__,
        )
        return _error_envelope(
            "MLX Whisper failed "
            f"({type(primary_error).__name__}); faster-whisper failed "
            f"({type(exc).__name__})",
            _FALLBACK_BACKEND,
        )


class MLXWhisperProvider(TranscriptionProvider):
    """MLX Whisper backend with a serialized faster-whisper fallback."""

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "MLX Whisper"

    def is_available(self) -> bool:
        """Expose macOS dispatch; unsupported CPUs use the lazy CPU fallback."""
        return sys.platform == "darwin"

    def list_models(self) -> List[Dict[str, Any]]:
        """Return the MLX Whisper model catalog for setup/model pickers."""
        return [dict(model) for model in _MODEL_CATALOG]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        """Describe local setup; neither backend needs credentials."""
        return {
            "name": "MLX Whisper",
            "badge": "macOS · local",
            "tag": (
                "Apple Silicon MLX Whisper with cached faster-whisper CPU "
                "fallback — no API key"
            ),
            "env_vars": [],
        }

    def transcribe(
        self,
        file_path: str,
        *,
        model: Optional[str] = None,
        language: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Transcribe audio, falling back without exposing backend details in text."""
        model_name = _optional_text(model) or DEFAULT_MODEL
        language_hint = _optional_text(language)
        initial_prompt = _optional_text(
            extra.get("prompt") or extra.get("initial_prompt")
        )

        with _TRANSCRIPTION_LOCK:
            try:
                mlx_whisper = _load_mlx_whisper()
                mlx_kwargs: Dict[str, Any] = {
                    "path_or_hf_repo": model_name,
                    "verbose": False,
                }
                if language_hint:
                    mlx_kwargs["language"] = language_hint
                if initial_prompt:
                    mlx_kwargs["initial_prompt"] = initial_prompt

                result = mlx_whisper.transcribe(file_path, **mlx_kwargs)
                transcript = _transcription_text(result)
                logger.info(
                    "Transcription succeeded (provider=%s, backend=%s)",
                    PROVIDER_NAME,
                    _MLX_BACKEND,
                )
                return _success_envelope(transcript, _MLX_BACKEND)
            except Exception as exc:  # noqa: BLE001 - fallback boundary
                logger.info(
                    "Primary transcription unavailable (provider=%s, "
                    "backend=%s, error=%s); using fallback backend=%s",
                    PROVIDER_NAME,
                    _MLX_BACKEND,
                    type(exc).__name__,
                    _FALLBACK_BACKEND,
                )
                return _transcribe_fallback(
                    file_path,
                    language=language_hint,
                    initial_prompt=initial_prompt,
                    primary_error=exc,
                )


def register(ctx: Any) -> None:
    """Register the lightweight provider; optional engines remain lazy."""
    ctx.register_transcription_provider(MLXWhisperProvider())


__all__ = ["MLXWhisperProvider", "register"]
