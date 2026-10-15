"""Live checks of the F1/F2 fixes on the real MediaTek hardware encoder.

Skipped unless the GPU-native runtime is installed and its hardware encoders
verify on this device. All mutable state (capability cache, circuit breaker,
telemetry) lives in a temp directory; the deployed runtime is only linked
read-only.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _real_binary(name):
    """The real executable, never the dispatcher shim that ``import tools`` puts first on PATH."""
    shim = str(REPO_ROOT / "gpu_native" / "shims" / "path")
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and os.path.abspath(d) != shim:
            cand = os.path.join(d, name)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return None


SHIM = REPO_ROOT / "gpu_native" / "shims" / "ffmpeg_dispatch.py"
RUNTIME = Path(os.environ.get("OM_GPU_RUNTIME_DIR") or "/data/data/com.termux/files/home/.openmontage-gpu")
FFMPEG = _real_binary("ffmpeg")
FFPROBE = _real_binary("ffprobe")

pytestmark = pytest.mark.skipif(
    not ((RUNTIME / "prefix" / "bin" / "ffmpeg").is_file() and FFMPEG and FFPROBE),
    reason="GPU-native runtime not installed")


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    base = tmp_path_factory.mktemp("live")
    home = base / "gpu-home"
    home.mkdir()
    for name in ("prefix", "browser", "bin", "realesrgan"):
        if (RUNTIME / name).exists():
            (home / name).symlink_to(RUNTIME / name, target_is_directory=True)
    if (RUNTIME / "manifest.json").exists():
        shutil.copyfile(RUNTIME / "manifest.json", home / "manifest.json")
    log = base / "exec.jsonl"
    env = dict(os.environ, OM_GPU_HOME=str(home), OM_GPU_MODE="auto", OM_GPU_OOP="0",
               OM_GPU_REAL_FFMPEG=FFMPEG, OM_GPU_EXEC_LOG=str(log))
    work = base / "work"
    work.mkdir()

    def run(args, **kw):
        return subprocess.run([sys.executable, "-I", "-S", str(SHIM), *args], env=env, cwd=work,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300, **kw)

    def events():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    # The hardware must really verify here, otherwise there is nothing live to check.
    src = work / "probe-src.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=128x128:rate=10:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    r = run(["-y", "-v", "error", "-i", str(src), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(work / "probe.mp4")])
    ev = events()
    if r.returncode != 0 or not ev or ev[-1].get("backend") != "gpu":
        pytest.skip(f"hardware encoder did not verify on this device: {ev[-1:] or r.stderr[-200:]}")
    return {"run": run, "events": events, "work": work, "env": env}


def probe(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt",
                        "-of", "json", str(path)], capture_output=True, text=True)
    return [(s["codec_type"], s["codec_name"], s.get("pix_fmt")) for s in json.loads(r.stdout)["streams"]]


@pytest.fixture(scope="module")
def src444(live):
    out = live["work"] / "src-444-10bit.mkv"
    # Large enough for the hardware encoders (the HEVC one rejects 64x64 and the
    # command then correctly runs on the CPU).
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=256x144:rate=10:duration=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv444p10le",
                    "-c:a", "aac", "-shortest", str(out)], check=True)
    return out


def test_f2_reported_command_yields_hevc_420_and_honest_telemetry(live, src444):
    """``-c copy -c:v libx265 -pix_fmt yuv420p`` was H.264 / yuv444p10le reported as an HEVC hardware encode."""
    out = live["work"] / "f2-reported.mp4"
    opts = ["-c", "copy", "-c:v", "libx265", "-pix_fmt", "yuv420p"]
    cpu = live["work"] / "f2-reported-cpu.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(src444), *opts, str(cpu)], check=True)
    r = live["run"](["-y", "-v", "error", "-i", str(src444), *opts, str(out)])
    assert r.returncode == 0, r.stderr
    assert probe(out) == probe(cpu) == [("video", "hevc", "yuv420p"), ("audio", "aac", None)]
    ev = live["events"]()[-1]
    assert ev["backend"] == "gpu" and ev["encoder"] == "hevc_mediacodec"
    assert ev["verified"] == {str(out): "hevc/yuv420p"}


def test_f2_a_later_copy_makes_the_video_a_copy_and_is_not_reported_as_hardware(live, src444):
    out = live["work"] / "f2-copy.mp4"
    n = len(live["events"]())
    r = live["run"](["-y", "-v", "error", "-i", str(src444), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c", "copy",
                     str(out)])
    assert r.returncode == 0, r.stderr
    assert probe(out) == [("video", "h264", "yuv444p10le"), ("audio", "aac", None)]   # copied, untouched
    assert not [e for e in live["events"]()[n:] if e.get("backend") == "gpu"]


def test_normal_workloads_still_encode_on_the_hardware_encoder(live, src444):
    out = live["work"] / "normal.mp4"
    r = live["run"](["-y", "-v", "error", "-i", str(src444), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23",
                     "-c:a", "aac", str(out)])
    assert r.returncode == 0, r.stderr
    assert probe(out) == [("video", "h264", "yuv420p"), ("audio", "aac", None)]
    ev = live["events"]()[-1]
    assert ev["backend"] == "gpu" and ev["encoder"] == "h264_mediacodec" and ev["device"].startswith("c2.mtk")


def test_f1a_a_failed_hardware_command_keeps_the_asset_named_by_a_file_url(live, src444):
    asset = live["work"] / "approved.mp4"
    shutil.copyfile(src444.with_suffix(".mkv"), asset)
    before = sha(asset)
    r = live["run"](["-y", "-v", "error", "-i", str(src444), "-c", "copy", f"file://{asset}",
                     "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(live["work"] / "no-dir" / "second.mp4")])
    assert r.returncode != 0
    assert sha(asset) == before
    assert not [p for p in live["work"].iterdir() if p.name.startswith(".om-gpu-")]


def test_f1b_an_implicit_hls_command_is_refused_and_the_old_segment_survives(live, src444):
    work = live["work"]
    seg = work / "production_000.ts"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(src444), "-c", "copy", str(seg)], check=True)
    blocked = work / "blocked.mp4"
    blocked.mkdir(exist_ok=True)
    before = sha(seg)
    r = live["run"](["-y", "-i", str(src444), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10", "-hls_time", "1",
                     "-hls_segment_filename", str(work / "production_%03d.ts"), str(work / "playlist.m3u8"),
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", str(blocked)])
    assert r.returncode == 78 and "refusing to run" in r.stderr
    assert sha(seg) == before and not (work / "playlist.m3u8").exists() and not (work / "production_001.ts").exists()
