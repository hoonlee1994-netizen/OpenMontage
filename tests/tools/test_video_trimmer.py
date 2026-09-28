"""Regression tests for video_trimmer copy-mode validation (B1).

Covers the ARM64 audit finding: a copy-mode `cut` on a sparse-keyframe
source previously produced an audio-only MP4 while returning
`ToolResult(success=True)`. The tool must never report a successful trim
of a video input when the output contains no video stream.

Corrective repair v2 additionally covers:
- copy-mode timing truthfulness (sparse 1.7-4.3 must not succeed at ~4.5s);
- unknown-probe fail-closed semantics (unknown != known audio-only).

Fixtures are tiny synthetic files (320x240, ~6s) built with ffmpeg.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.video.video_trimmer import (
    COPY_DURATION_TOLERANCE_SEC,
    VideoTrimmer,
    probe_codec_types,
    probe_output_duration,
)


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


def _independent_duration(path: Path) -> float | None:
    """Independent ffprobe format-duration check (not via the tool)."""
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        return None
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


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
# B1 corrective v2: copy-mode timing truthfulness + tolerance definition
# ------------------------------------------------------------------


def test_copy_tolerance_is_explicit_and_narrow():
    """Tolerance must reject a 1.9s overshoot on a 2.6s request."""
    assert COPY_DURATION_TOLERANCE_SEC == 0.5
    assert 1.9 > COPY_DURATION_TOLERANCE_SEC
    # Sanity: tolerance is positive but tight (packet jitter is ~0.3s max).
    assert 0 < COPY_DURATION_TOLERANCE_SEC <= 0.5


@needs_ffmpeg
def test_sparse_copy_window_truthful_or_fails_with_reencode(
    tmp_path: Path, sparse_av: Path
):
    """Regression 1: requested 1.7-4.3 (2.6s) must never succeed at ~4.5s."""
    out = tmp_path / "sparse_window.mp4"
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
    requested = 4.3 - 1.7
    if result.success:
        assert out.is_file()
        assert "video" in _streams_of(out)
        actual = _independent_duration(out)
        assert actual is not None
        assert abs(actual - requested) <= COPY_DURATION_TOLERANCE_SEC, (
            f"copy success claimed window {actual}s vs requested {requested}s"
        )
        # Explicit guard against the old ~4.5s false-success artifact.
        assert actual < 3.5, f"overshoot artifact returned as success: {actual}s"
        assert result.data["duration_delta"] <= COPY_DURATION_TOLERANCE_SEC
    else:
        err = (result.error or "").lower()
        assert "re-encode" in err and "libx264" in err
        assert "tolerance" in err
        assert not out.exists()
        assert result.data["requested_duration"] == pytest.approx(requested)
        assert result.data["actual_duration"] is not None
        assert result.data["duration_delta"] > COPY_DURATION_TOLERANCE_SEC


@needs_ffmpeg
def test_dense_copy_window_within_tolerance(tmp_path: Path, dense_av: Path):
    """Regression 2: keyframe-aligned/dense copy stays a supported success."""
    out = tmp_path / "dense_window.mp4"
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
    actual = _independent_duration(out)
    assert actual is not None
    assert abs(actual - 3.0) <= COPY_DURATION_TOLERANCE_SEC
    assert result.data["input_probe"] == "known_video"
    assert result.data["output_has_video"] is True


@needs_ffmpeg
def test_reencode_cut_duration_near_exact(tmp_path: Path, sparse_av: Path):
    """Regression 3: re-encode stays frame-accurate on the failing window."""
    out = tmp_path / "reenc_exact.mp4"
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
    assert "video" in _streams_of(out)
    actual = _independent_duration(out)
    assert actual is not None
    assert abs(actual - 2.6) <= 0.3, f"re-encode drifted: {actual}s vs 2.6s"


# ------------------------------------------------------------------
# B1 corrective v2: stream-preservation / probe-state semantics
# ------------------------------------------------------------------


@needs_ffmpeg
def test_unknown_probe_audio_only_never_succeeds(
    tmp_path: Path, dense_av: Path, monkeypatch
):
    """Regression 5: input probe unknown + audio-only output MUST fail."""
    import tools.video.video_trimmer as vt_mod

    real_probe = vt_mod.probe_codec_types

    def fake_probe(path: Path):
        if path.name == "unknown_in.mp4":
            return None  # unknown / inconclusive input
        if path.name == "unknown_out.mp4":
            return {"audio"}  # provably audio-only output
        return real_probe(path)

    monkeypatch.setattr(vt_mod, "probe_codec_types", fake_probe)

    src = tmp_path / "unknown_in.mp4"
    shutil.copy(dense_av, src)
    out = tmp_path / "unknown_out.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(src),
            "output_path": str(out),
            "start_seconds": 0,
            "end_seconds": 2,
            "codec": "copy",
        }
    )
    assert not result.success, "unknown-probe audio-only output claimed success"
    assert not out.exists(), "invalid artifact must be removed"
    err = (result.error or "").lower()
    assert "unknown" in err
    assert "fail-closed" in err or "cannot be established" in err
    assert "distinct from positively identified audio-only" in err
    assert result.data["input_probe"] == "unknown"
    assert result.data["input_streams"] is None
    assert result.data["output_streams"] == ["audio"]


@needs_ffmpeg
def test_unknown_probe_with_video_output_can_succeed(
    tmp_path: Path, dense_av: Path, monkeypatch
):
    """Unknown input + provably-video output: success means output has video."""
    import tools.video.video_trimmer as vt_mod

    real_probe = vt_mod.probe_codec_types

    def fake_probe(path: Path):
        if path.name == "mystery_in.mp4":
            return None
        return real_probe(path)

    monkeypatch.setattr(vt_mod, "probe_codec_types", fake_probe)

    src = tmp_path / "mystery_in.mp4"
    shutil.copy(dense_av, src)
    out = tmp_path / "mystery_out.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(src),
            "output_path": str(out),
            "start_seconds": 0,
            "end_seconds": 2,
            "codec": "copy",
        }
    )
    # Valid dense window: duration gate passes, output provably has video.
    assert result.success, result.error
    assert result.data["input_probe"] == "unknown"
    assert result.data["output_has_video"] is True
    assert "video" in _streams_of(out)


@needs_ffmpeg
def test_known_audio_only_probe_state_and_extension_independence(
    tmp_path: Path, audio_only: Path
):
    """Regression 6: positively identified audio-only stays supported.

    Stream type comes from ffprobe, never from the filename extension.
    """
    assert result_probe_state(audio_only) == "known_audio_only"
    # Rename .m4a -> .mp4: behavior must follow streams, not the extension.
    disguised = tmp_path / "disguised.mp4"
    shutil.copy(audio_only, disguised)
    out = tmp_path / "audio_renamed_cut.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(disguised),
            "output_path": str(out),
            "start_seconds": 0,
            "end_seconds": 2,
            "codec": "copy",
        }
    )
    assert result.success, result.error
    assert out.is_file()
    assert result.data["input_probe"] == "known_audio_only"
    assert result.data["output_has_video"] is False


def result_probe_state(path: Path) -> str:
    types = probe_codec_types(path)
    if types is None or len(types) == 0:
        return "unknown"
    return "known_audio_only" if "video" not in types else "known_video"


@needs_ffmpeg
def test_output_probe_unknown_fails_but_leaves_file(
    tmp_path: Path, dense_av: Path, monkeypatch
):
    """Regression 7a: output probe inconclusive -> fail, leave file."""
    import tools.video.video_trimmer as vt_mod

    real_probe = vt_mod.probe_codec_types

    def fake_probe(path: Path):
        if path.name == "unverifiable.mp4":
            return None
        return real_probe(path)

    monkeypatch.setattr(vt_mod, "probe_codec_types", fake_probe)

    out = tmp_path / "unverifiable.mp4"
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
    assert out.exists(), "inconclusive output must be left for inspection"
    assert result.data["output_probe"] == "unknown"
    assert "manual inspection" in (result.error or "").lower()


@needs_ffmpeg
def test_probe_failure_states_are_structured_and_distinct(
    tmp_path: Path, dense_av: Path, audio_only: Path
):
    """Regression 7b: unknown is a distinct state from known audio-only."""
    assert probe_codec_types(dense_av) is not None
    assert "video" in (probe_codec_types(dense_av) or set())
    assert probe_codec_types(audio_only) == {"audio"}
    assert probe_output_duration(dense_av) is not None


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
