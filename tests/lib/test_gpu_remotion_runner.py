"""Decision logic of the GPU-first Remotion runner (lib/gpu/remotion.run_render)."""
from __future__ import annotations

import subprocess

import pytest

from lib.gpu import remotion, runtime, telemetry

BASE = ["npx", "remotion", "render", "src/index.tsx", "Explainer", "/out.mp4", "--props=/p.json"]


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_GPU_HOME", str(tmp_path / "gpu-home"))
    monkeypatch.setattr(telemetry, "record", lambda ev: None)
    monkeypatch.setattr(remotion, "binaries_dir", lambda: tmp_path / "remotion-bin")
    monkeypatch.setattr(remotion, "cpu_browser", lambda: "/usr/bin/chromium")
    monkeypatch.setenv("OM_GPU_MODE", "auto")


class FakeWatch:
    verdict_value = ("gpu", [])

    def __init__(self, render_id):
        self.seen = {"1": ["--use-angle=gles-egl", "--use-gl=angle"]}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def verdict(self):
        return FakeWatch.verdict_value


def _runner(fail_gpu=False, fail_cpu=False):
    calls = []

    def run(cmd, timeout=None, cwd=None):
        calls.append(cmd)
        gpu = any(str(runtime.chromium_launcher()) in c for c in cmd)
        if (gpu and fail_gpu) or (not gpu and fail_cpu):
            raise subprocess.CalledProcessError(1, cmd, stderr="boom")
    return run, calls


def test_gpu_render_uses_mali_browser_and_hardware_encoder_hook(monkeypatch):
    monkeypatch.setattr(remotion, "plan", lambda: {"gpu": True, "renderer": "ANGLE (ARM, Mali-G720 MC8)"})
    monkeypatch.setattr(remotion, "_GpuProcessWatch", FakeWatch)
    FakeWatch.verdict_value = ("gpu", [])
    run, calls = _runner()
    rec = remotion.run_render(BASE, cwd=None, timeout=10, runner=run)
    assert rec["backend"] == "gpu" and len(calls) == 1
    cmd = calls[0]
    # Positionals untouched, our flags before the caller's first --option.
    assert cmd[:6] == BASE[:6] and cmd[-1] == "--props=/p.json"
    assert f"--browser-executable={runtime.chromium_launcher()}" in cmd
    assert any(c.startswith("--binaries-directory=") for c in cmd)


def test_gpu_failure_falls_back_to_cpu_and_is_recorded(monkeypatch):
    monkeypatch.setattr(remotion, "plan", lambda: {"gpu": True, "renderer": "mali"})
    monkeypatch.setattr(remotion, "_GpuProcessWatch", FakeWatch)
    run, calls = _runner(fail_gpu=True)
    rec = remotion.run_render(BASE, cwd=None, timeout=10, runner=run)
    assert rec["backend"] == "cpu" and len(calls) == 2
    assert "--browser-executable=/usr/bin/chromium" in calls[1]
    assert not any(c.startswith("--binaries-directory=") for c in calls[1])
    assert rec["attempts"][0]["backend"] == "gpu"
    # A GPU-specific failure (CPU succeeded) counts toward the breaker.
    assert len(runtime._read_health()["chromium-gpu"]["failures"]) == 1
    assert runtime.healthy("chromium-gpu")  # breaker opens only after 3


def test_degraded_gpu_render_is_redone_on_cpu(monkeypatch):
    monkeypatch.setattr(remotion, "plan", lambda: {"gpu": True, "renderer": "mali"})
    monkeypatch.setattr(remotion, "_GpuProcessWatch", FakeWatch)
    FakeWatch.verdict_value = ("gpu-degraded", ["--use-gl=disabled"])
    try:
        run, calls = _runner()
        rec = remotion.run_render(BASE, cwd=None, timeout=10, runner=run)
    finally:
        FakeWatch.verdict_value = ("gpu", [])
    assert rec["backend"] == "cpu" and len(calls) == 2
    assert rec["attempts"][0]["backend"] == "gpu-degraded"


def test_require_mode_never_falls_back(monkeypatch):
    monkeypatch.setenv("OM_GPU_MODE", "require")
    monkeypatch.setattr(remotion, "plan", lambda: {"gpu": True, "renderer": "mali"})
    monkeypatch.setattr(remotion, "_GpuProcessWatch", FakeWatch)
    run, calls = _runner(fail_gpu=True)
    with pytest.raises(subprocess.CalledProcessError):
        remotion.run_render(BASE, cwd=None, timeout=10, runner=run)
    assert len(calls) == 1


def test_unavailable_gpu_goes_straight_to_cpu_with_reason(monkeypatch):
    monkeypatch.setattr(remotion, "plan", lambda: {"gpu": False, "reason": "GPU browser not verified"})
    run, calls = _runner()
    rec = remotion.run_render(BASE, cwd=None, timeout=10, runner=run)
    assert rec == {**rec, "backend": "cpu", "fallback_reason": "GPU browser not verified"}
    assert len(calls) == 1


def test_cpu_mode_plan(monkeypatch):
    monkeypatch.setenv("OM_GPU_MODE", "cpu")
    assert remotion.plan()["gpu"] is False
