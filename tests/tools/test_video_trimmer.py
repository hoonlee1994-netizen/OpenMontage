"""Regression tests for video_trimmer copy-mode validation (B1).

Covers the ARM64 audit finding: a copy-mode `cut` on a sparse-keyframe
source previously produced an audio-only MP4 while returning
`ToolResult(success=True)`. The tool must never report a successful trim
of a video input when the output contains no video stream.

Fixtures are tiny synthetic files (320x240, ~6s) built with ffmpeg.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tools.video.video_trimmer import VideoTrimmer, probe_codec_types


def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


needs_ffmpeg = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg/ffprobe required")


def _build_av_fixture(path: Path, keyframe_interval: int) -> Path:
    """Build a tiny video+audio MP4 with a controlled GOP size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-preset", "veryfast",
            "-g", str(keyframe_interval), "-bf", "3",
            "-c:a", "aac",
            "-shortest", str(path),
        ],
        capture_output=True, check=True, timeout=120,
    )
    return path


@pytest.fixture()
def dense_av(tmp_path: Path) -> Path:
    """Frequent keyframes: copy-mode cuts are safe here."""
    return _build_av_fixture(tmp_path / "dense.mp4", keyframe_interval=12)


@pytest.fixture()
def sparse_av(tmp_path: Path) -> Path:
    """Sparse keyframes: the previously dangerous copy-mode condition."""
    return _build_av_fixture(tmp_path / "sparse.mp4", keyframe_interval=250)


@pytest.fixture()
def audio_only(tmp_path: Path) -> Path:
    out = tmp_path / "audio_only.m4a"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
            "-c:a", "aac", str(out),
        ],
        capture_output=True, check=True, timeout=60,
    )
    return out


def _streams_of(path: Path) -> set[str]:
    return probe_codec_types(path) or set()


# ------------------------------------------------------------------
# B1: re-encode and copy-mode success paths
# ------------------------------------------------------------------


@needs_ffmpeg
def test_reencode_cut_succeeds_with_video(tmp_path: Path, sparse_av: Path):
    out = tmp_path / "reenc.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(sparse_av),
            "output_path": str(out),
            "start_seconds": 1.7,
            "end_seconds": 4.3,
            "codec": "libx264",
        }
    )
    assert result.success, result.error
    assert out.is_file()
    assert "video" in _streams_of(out)


@needs_ffmpeg
def test_copy_mode_valid_case_succeeds_with_video(tmp_path: Path, dense_av: Path):
    out = tmp_path / "copy_ok.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(dense_av),
            "output_path": str(out),
            "start_seconds": 0,
            "end_seconds": 3,
            "codec": "copy",
        }
    )
    assert result.success, result.error
    assert out.is_file()
    assert "video" in _streams_of(out)


@needs_ffmpeg
def test_copy_mode_dangerous_cut_never_claims_audio_only_success(
    tmp_path: Path, sparse_av: Path
):
    """Property test: success=True implies the output has a video stream.

    Under the old output-seeking invocation this exact cut produced an
    audio-only MP4 with success=True. The repaired input-seeking
    invocation preserves video; if any future cut still drops video, the
    tool must fail instead of lying.
    """
    out = tmp_path / "copy_sparse.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(sparse_av),
            "output_path": str(out),
            "start_seconds": 1.7,
            "end_seconds": 4.3,
            "codec": "copy",
        }
    )
    if result.success:
        assert out.is_file()
        assert "video" in _streams_of(out), (
            "copy-mode cut reported success but output has no video stream"
        )
    else:
        err = (result.error or "").lower()
        assert "re-encode" in err or "libx264" in err
        assert not out.exists(), "invalid copy output must be removed on failure"


@needs_ffmpeg
def test_audio_only_input_copy_cut_stays_successful(tmp_path: Path, audio_only: Path):
    """The video-stream requirement applies to video inputs only."""
    assert "video" not in _streams_of(audio_only)
    out = tmp_path / "audio_cut.m4a"
    result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(audio_only),
            "output_path": str(out),
            "start_seconds": 0,
            "end_seconds": 2,
            "codec": "copy",
        }
    )
    assert result.success, result.error
    assert out.is_file()


# ------------------------------------------------------------------
# B1: structured failure semantics (deterministic, mocked probe)
# ------------------------------------------------------------------


@needs_ffmpeg
def test_copy_failure_semantics_are_structured_and_remove_artifact(
    tmp_path: Path, dense_av: Path, monkeypatch
):
    """Force the dangerous condition: output provably lacks video."""
    import tools.video.video_trimmer as vt_mod

    real_probe = vt_mod.probe_codec_types

    def fake_probe(path: Path):
        if path.name == "forced.mp4":
            return {"audio"}
        return real_probe(path)

    monkeypatch.setattr(vt_mod, "probe_codec_types", fake_probe)

    out = tmp_path / "forced.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(dense_av),
            "output_path": str(out),
            "start_seconds": 0,
            "end_seconds": 2,
            "codec": "copy",
        }
    )
    assert not result.success
    err = result.error or ""
    assert "no video stream" in err
    assert "libx264" in err, "failure must direct the caller to re-encode mode"
    assert "copy" in err.lower()
    # Structured data, not just a string.
    assert result.data["operation"] == "cut"
    assert result.data["codec"] == "copy"
    assert result.data["output_streams"] == ["audio"]
    assert "video" in (result.data["input_streams"] or [])
    # No corrupt success artifact left behind.
    assert not out.exists()


# ------------------------------------------------------------------
# B1: unrelated operations unchanged
# ------------------------------------------------------------------


@needs_ffmpeg
def test_speed_behavior_unchanged(tmp_path: Path, dense_av: Path):
    out = tmp_path / "fast.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "speed",
            "input_path": str(dense_av),
            "output_path": str(out),
            "speed_factor": 2.0,
        }
    )
    assert result.success, result.error
    streams = _streams_of(out)
    assert "video" in streams and "audio" in streams
    assert result.data["speed_factor"] == 2.0


@needs_ffmpeg
def test_concat_behavior_unchanged(tmp_path: Path, dense_av: Path):
    out = tmp_path / "joined.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(dense_av)}, {"input_path": str(dense_av)}],
        }
    )
    assert result.success, result.error
    assert result.data["segment_count"] == 2
    assert "video" in _streams_of(out)


def test_cut_missing_input_still_fails():
    result = VideoTrimmer().execute(
        {"operation": "cut", "input_path": "/nonexistent/file.mp4"}
    )
    assert not result.success
