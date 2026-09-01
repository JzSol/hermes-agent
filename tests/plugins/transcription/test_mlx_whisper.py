"""Contract tests for the optional MLX Whisper transcription plugin."""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace

import yaml


PLUGIN_DIR = (
    Path(__file__).resolve().parents[3] / "plugins" / "transcription" / "mlx-whisper"
)


def load_plugin():
    """Load the hyphenated plugin path as an isolated module instance."""
    module_name = f"test_mlx_whisper_plugin_{time.monotonic_ns()}"
    spec = importlib.util.spec_from_file_location(
        module_name, PLUGIN_DIR / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_declares_bounded_optional_dependency():
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert manifest["name"] == "mlx-whisper"
    assert manifest["kind"] == "backend"
    assert manifest["platforms"] == ["macos"]
    assert manifest["python_dependencies"] == ["mlx-whisper>=0.4.3,<0.5"]
    from tools.lazy_deps import LAZY_DEPS

    assert LAZY_DEPS["stt.mlx_whisper"] == ("mlx-whisper==0.4.3",)


def test_primary_backend_receives_model_language_and_prompt(monkeypatch):
    calls = []
    fake_mlx = ModuleType("mlx_whisper")

    def transcribe(file_path, **kwargs):
        calls.append((file_path, kwargs))
        return {"text": "  Adam heard this clearly.  "}

    fake_mlx.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake_mlx)
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "_mlx_supported_host", lambda: True)

    result = plugin.MLXWhisperProvider().transcribe(
        "/tmp/adam.wav",
        language="en",
        prompt="Adam, Janis, Tailscale",
    )

    assert result == {
        "success": True,
        "transcript": "Adam heard this clearly.",
        "provider": "mlx-whisper",
        "backend": "mlx-whisper",
    }
    assert calls == [
        (
            "/tmp/adam.wav",
            {
                "path_or_hf_repo": plugin.DEFAULT_MODEL,
                "verbose": False,
                "language": "en",
                "initial_prompt": "Adam, Janis, Tailscale",
            },
        )
    ]


def test_failed_primary_uses_one_cached_faster_whisper_model(monkeypatch):
    fake_mlx = ModuleType("mlx_whisper")
    fake_mlx.transcribe = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("Metal unavailable")
    )
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake_mlx)

    model_creations = []
    transcribe_calls = []

    class FakeWhisperModel:
        def __init__(self, model, *, device, compute_type):
            model_creations.append((model, device, compute_type))

        def transcribe(self, file_path, **kwargs):
            transcribe_calls.append((file_path, kwargs))
            return ([SimpleNamespace(text=" fallback transcript ")], None)

    plugin = load_plugin()
    monkeypatch.setattr(plugin, "_mlx_supported_host", lambda: True)
    monkeypatch.setattr(plugin, "_module_is_available", lambda _name: True)
    monkeypatch.setattr(
        "tools.transcription_tools._load_local_whisper_model",
        FakeWhisperModel,
    )
    provider = plugin.MLXWhisperProvider()

    first = provider.transcribe("/tmp/one.wav", language="en")
    second = provider.transcribe("/tmp/two.wav", initial_prompt="Ray-Ban")

    assert first["backend"] == "faster-whisper"
    assert first["transcript"] == "fallback transcript"
    assert second["success"] is True
    assert model_creations == [("small", "cpu", "int8")]
    assert transcribe_calls == [
        ("/tmp/one.wav", {"beam_size": 1, "vad_filter": True, "language": "en"}),
        (
            "/tmp/two.wav",
            {"beam_size": 1, "vad_filter": True, "initial_prompt": "Ray-Ban"},
        ),
    ]


def test_missing_mlx_dependency_uses_allowlisted_lazy_install(monkeypatch):
    fake_mlx = ModuleType("mlx_whisper")
    fake_mlx.transcribe = lambda *_args, **_kwargs: {"text": "installed lazily"}
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "_mlx_supported_host", lambda: True)
    monkeypatch.setattr(plugin, "_module_is_available", lambda _name: False)
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake_mlx)
    ensure_calls = []
    monkeypatch.setattr(
        "tools.lazy_deps.ensure",
        lambda feature, *, prompt: ensure_calls.append((feature, prompt)),
    )

    result = plugin.MLXWhisperProvider().transcribe("/tmp/adam.wav")

    assert result["success"] is True
    assert ensure_calls == [("stt.mlx_whisper", False)]


def test_both_backend_failures_are_bounded_and_preserve_error_types(monkeypatch):
    fake_mlx = ModuleType("mlx_whisper")
    fake_mlx.transcribe = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ValueError("private primary details")
    )
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake_mlx)
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "_mlx_supported_host", lambda: True)
    monkeypatch.setattr(
        plugin,
        "_load_fallback_model",
        lambda: (_ for _ in ()).throw(OSError("private fallback details")),
    )

    result = plugin.MLXWhisperProvider().transcribe("/tmp/adam.wav")

    assert result["success"] is False
    assert result["error"] == (
        "MLX Whisper failed (ValueError); faster-whisper failed (OSError)"
    )
    assert "private" not in result["error"]


def test_availability_keeps_lazy_install_dispatch_reachable(monkeypatch):
    plugin = load_plugin()
    monkeypatch.setattr(plugin.sys, "platform", "darwin")
    monkeypatch.setattr(plugin, "_module_is_available", lambda _name: False)

    assert plugin.MLXWhisperProvider().is_available() is True


def test_unsupported_architecture_skips_mlx_install_and_uses_fallback(monkeypatch):
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "_mlx_supported_host", lambda: False)
    ensure_calls = []
    monkeypatch.setattr(
        "tools.lazy_deps.ensure",
        lambda feature, *, prompt: ensure_calls.append((feature, prompt)),
    )
    fallback = SimpleNamespace(
        transcribe=lambda *_args, **_kwargs: (
            [SimpleNamespace(text="CPU fallback")],
            None,
        )
    )
    monkeypatch.setattr(plugin, "_load_fallback_model", lambda: fallback)

    result = plugin.MLXWhisperProvider().transcribe("/tmp/adam.wav")

    assert result["backend"] == "faster-whisper"
    assert result["transcript"] == "CPU fallback"
    assert ensure_calls == []


def test_missing_fallback_uses_existing_faster_whisper_lazy_path(monkeypatch):
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "_module_is_available", lambda _name: False)
    ensure_calls = []
    monkeypatch.setattr(
        "tools.lazy_deps.ensure",
        lambda feature, *, prompt: ensure_calls.append((feature, prompt)),
    )
    sentinel = object()
    monkeypatch.setattr(
        "tools.transcription_tools._load_local_whisper_model",
        lambda *_args, **_kwargs: sentinel,
    )

    assert plugin._load_fallback_model() is sentinel
    assert ensure_calls == [("stt.faster_whisper", False)]


def test_lazy_updater_skips_mlx_on_intel_or_rosetta(monkeypatch):
    from tools import lazy_deps

    monkeypatch.setattr(lazy_deps.sys, "platform", "darwin")
    monkeypatch.setattr(lazy_deps.platform, "machine", lambda: "x86_64")
    reason = lazy_deps._unsupported_feature_reason("stt.mlx_whisper")

    assert reason is not None
    assert "native Apple Silicon" in reason


def test_transcriptions_are_serialized(monkeypatch):
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0
    fake_mlx = ModuleType("mlx_whisper")

    def transcribe(file_path, **_kwargs):
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.02)
        with state_lock:
            active -= 1
        return {"text": file_path}

    fake_mlx.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake_mlx)
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "_mlx_supported_host", lambda: True)
    provider = plugin.MLXWhisperProvider()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(provider.transcribe, ["a", "b", "c", "d"]))

    assert maximum_active == 1
    assert [result["transcript"] for result in results] == ["a", "b", "c", "d"]


def test_registration_is_lightweight_and_uses_provider_contract():
    plugin = load_plugin()
    registered = []
    plugin.register(SimpleNamespace(register_transcription_provider=registered.append))

    assert len(registered) == 1
    provider = registered[0]
    assert provider.name == "mlx-whisper"
    assert provider.default_model() == plugin.DEFAULT_MODEL
    assert provider.get_setup_schema()["env_vars"] == []
