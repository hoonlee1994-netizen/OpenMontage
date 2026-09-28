"""Regression tests for video_trimmer copy-mode validation (B1).

Covers the ARM64 audit finding: a copy-mode `cut` on a sparse-keyframe
source previously produced an audio-only MP4 while returning
`ToolResult(success=True)`. The tool must never report a successful trim
of a video input when the output contains no video stream.

Corrective repair v2 additionally covers:
- copy-mode timing truthfulness (sparse 1.7-4.3 must not succeed at ~4.5s);
- unknown-probe fail-closed semantics (unknown != known audio-only).

Corrective repair v3 (truthful stream-copy semantics):
- interval validation before FFmpeg (end > start, no artifact);
- keyframe-alignment gate for video copy (nonzero start must match an
  actual ffprobe keyframe within 0.05 s; start==0 eligible);
- bounded proportional duration gate min(0.30, requested*0.10);
- input-unknown always fails closed (never equivalent to audio-only);
- empty output stream set always fails (including audio-only);
- cleanup outcome reported truthfully.

Fixtures are tiny synthetic files (320x240, ~6s) built with ffmpeg.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.video.video_trimmer import (
    COPY_ABSOLUTE_CAP_SEC,
    COPY_DURATION_TOLERANCE_SEC,
    COPY_RELATIVE_TOLERANCE,
    KEYFRAME_ALIGN_TOLERANCE_SEC,
    VideoTrimmer,
    copy_allowed_delta,
    probe_codec_types,
    probe_keyframe_times,
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
    tool must fail instead of lying. Under v3 the sparse 1.7 start is
    additionally rejected pre-FFmpeg by the keyframe gate.
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
        # Pre-FFmpeg keyframe rejection leaves no artifact; post-FFmpeg
        # duration/stream rejection must remove the invalid artifact.
        assert not out.exists(), "invalid copy output must be absent on failure"


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
# B1 corrective v3: bounded proportional tolerance definition
# ------------------------------------------------------------------


def test_copy_tolerance_is_bounded_proportional():
    """Proportional gate: min(0.30, requested*0.10).

    Justified from packet behavior: legitimate keyframe-aligned 3 s copies
    overshoot ~0.13-0.22 s (AAC frame ~0.023 s + video frame ~0.033 s +
    muxing), so cap 0.30 passes them with margin while staying below one
    dense GOP (0.40 s). Relative 10 % keeps short windows strict.
    """
    assert COPY_ABSOLUTE_CAP_SEC == pytest.approx(0.30)
    assert COPY_RELATIVE_TOLERANCE == pytest.approx(0.10)
    assert KEYFRAME_ALIGN_TOLERANCE_SEC == pytest.approx(0.05)
    # Deprecated alias still resolves to the cap for import compatibility.
    assert COPY_DURATION_TOLERANCE_SEC == pytest.approx(COPY_ABSOLUTE_CAP_SEC)
    # Required acceptance points:
    # 3.0 s with ~0.17 s deviation passes.
    assert copy_allowed_delta(3.0) >= 0.17
    assert copy_allowed_delta(3.0) == pytest.approx(0.30)
    # 2.6 s -> ~4.43 s (delta ~1.83) must fail.
    assert copy_allowed_delta(2.6) < 1.83
    assert copy_allowed_delta(2.6) == pytest.approx(0.26)
    # 0.8 s -> ~1.275 s (delta 0.475, 59 %) must fail.
    assert copy_allowed_delta(0.8) < 0.475
    assert copy_allowed_delta(0.8) == pytest.approx(0.08)
    # Keyframe tolerance is narrowly frame-level, not GOP-level.
    assert 0 < KEYFRAME_ALIGN_TOLERANCE_SEC < 0.40
    assert KEYFRAME_ALIGN_TOLERANCE_SEC <= 0.10


@needs_ffmpeg
def test_sparse_copy_window_truthful_or_fails_with_reencode(
    tmp_path: Path, sparse_av: Path
):
    """Regression 1: requested 1.7-4.3 (2.6s) must never succeed at ~4.5s.

    Under v3 the sparse 1.7 start is rejected pre-FFmpeg by the
    keyframe-alignment gate (nearest keyframe 0.0, distance 1.7 s).
    Either a pre-FFmpeg keyframe failure or a post-FFmpeg duration
    failure is acceptable, but success at ~4.5 s is not.
    """
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
    allowed = copy_allowed_delta(requested)
    if result.success:
        assert out.is_file()
        assert "video" in _streams_of(out)
        actual = _independent_duration(out)
        assert actual is not None
        assert abs(actual - requested) <= allowed, (
            f"copy success claimed window {actual}s vs requested {requested}s"
        )
        # Explicit guard against the old ~4.5s false-success artifact.
        assert actual < 3.5, f"overshoot artifact returned as success: {actual}s"
        assert result.data["duration_delta"] <= allowed
    else:
        err = (result.error or "").lower()
        assert "re-encode" in err and "libx264" in err
        assert not out.exists()
        assert result.data["requested_duration"] == pytest.approx(requested)
        # Pre-FFmpeg keyframe rejection has no duration probe; post-FFmpeg
        # duration rejection must report the overshoot.
        if "keyframe" in err:
            assert result.data.get("keyframe_aligned") is False
        else:
            assert "tolerance" in err or "allowed" in err
            assert result.data["actual_duration"] is not None
            assert result.data["duration_delta"] > allowed


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
    allowed = copy_allowed_delta(3.0)
    assert abs(actual - 3.0) <= allowed
    assert result.data["input_probe"] == "known_video"
    assert result.data["output_has_video"] is True
    assert result.data["allowed_tolerance"] == pytest.approx(allowed)


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
    """Regression 5: input probe unknown MUST fail closed before FFmpeg.

    Under v3 an unknown input can never support a media-preservation claim,
    so the tool rejects before running FFmpeg and creates no artifact. The
    unknown state stays distinct from positively identified audio-only.
    """
    import tools.video.video_trimmer as vt_mod

    real_probe = vt_mod.probe_codec_types

    def fake_probe(path: Path):
        if path.name == "unknown_in.mp4":
            return None  # unknown / inconclusive input
        if path.name == "unknown_out.mp4":
            return {"audio"}  # provably audio-only output (never reached)
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
    assert not out.exists(), "pre-FFmpeg rejection must create no artifact"
    err = (result.error or "").lower()
    assert "unknown" in err
    assert "fail-closed" in err or "cannot be established" in err
    assert "distinct from positively identified audio-only" in err
    assert "never equivalent" in err
    assert result.data["input_probe"] == "unknown"
    assert result.data["input_streams"] is None


@needs_ffmpeg
def test_unknown_input_probe_fails_closed_even_for_video_output(
    tmp_path: Path, dense_av: Path, monkeypatch
):
    """v3: unknown input fails closed even when output would have video.

    Supersedes the v2 expectation that unknown+video could succeed. An
    autonomous agent must never consume an unverified media-preservation
    claim, so unknown input is rejected pre-FFmpeg with no artifact.
    """
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
    assert not result.success, "unknown input must not claim success"
    assert not out.exists(), "pre-FFmpeg rejection must create no artifact"
    err = (result.error or "").lower()
    assert "unknown" in err
    assert "fail-closed" in err or "cannot be established" in err
    assert result.data["input_probe"] == "unknown"


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
# B1 corrective v3: truthful stream-copy semantics (15 acceptance points)
# ------------------------------------------------------------------


@needs_ffmpeg
def test_v3_invalid_end_eq_start_rejected_before_ffmpeg(
    tmp_path: Path, dense_av: Path, monkeypatch
):
    """v3.1: end == start rejected before FFmpeg, no artifact."""
    import tools.video.video_trimmer as vt_mod

    def _must_not_run(cmd, **kwargs):
        raise AssertionError("FFmpeg must not run for an invalid interval")

    monkeypatch.setattr(VideoTrimmer, "run_command", _must_not_run)
    out = tmp_path / "invalid_eq.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(dense_av),
            "output_path": str(out),
            "start_seconds": 2,
            "end_seconds": 2,
            "codec": "copy",
        }
    )
    assert not result.success
    assert not out.exists(), "invalid interval must not create an artifact"
    err = (result.error or "").lower()
    assert "end_seconds" in err and "start_seconds" in err
    assert "greater than" in err
    assert result.data["requested_duration"] == pytest.approx(0.0)


@needs_ffmpeg
def test_v3_invalid_end_lt_start_rejected_before_ffmpeg(
    tmp_path: Path, dense_av: Path, monkeypatch
):
    """v3.2: end < start rejected before FFmpeg, no artifact."""
    import tools.video.video_trimmer as vt_mod  # noqa: F401

    def _must_not_run(cmd, **kwargs):
        raise AssertionError("FFmpeg must not run for an invalid interval")

    monkeypatch.setattr(VideoTrimmer, "run_command", _must_not_run)
    out = tmp_path / "invalid_lt.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(dense_av),
            "output_path": str(out),
            "start_seconds": 3,
            "end_seconds": 2,
            "codec": "copy",
        }
    )
    assert not result.success
    assert not out.exists()
    assert result.data["requested_duration"] == pytest.approx(-1.0)
    assert "greater than" in (result.error or "").lower()


@needs_ffmpeg
def test_v3_sparse_gop_nonkeyframe_copy_rejected(tmp_path: Path, sparse_av: Path):
    """v3.3: sparse-GOP non-keyframe video copy start rejected pre-FFmpeg."""
    kfs = probe_keyframe_times(sparse_av)
    assert kfs is not None and len(kfs) == 1
    out = tmp_path / "sparse_nk.mp4"
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
    assert not result.success
    assert not out.exists(), "keyframe rejection must create no artifact"
    err = (result.error or "").lower()
    assert "keyframe" in err
    assert "libx264" in err and "re-encode" in err
    assert "silently" in err or "nothing was silently" in err or "not" in err
    assert result.data["keyframe_aligned"] is False
    assert result.data["keyframe_distance"] is not None
    assert result.data["keyframe_distance"] > KEYFRAME_ALIGN_TOLERANCE_SEC


@needs_ffmpeg
def test_v3_dense_midgop_nonkeyframe_copy_rejected(tmp_path: Path, dense_av: Path):
    """v3.3b: dense 1.7 (0.10 s from 1.6) rejected by the 0.05 s gate.

    The proportional duration gate alone would pass this window, so this
    proves the keyframe gate is load-bearing beyond duration.
    """
    out = tmp_path / "dense_nk.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(dense_av),
            "output_path": str(out),
            "start_seconds": 1.7,
            "end_seconds": 4.3,
            "codec": "copy",
        }
    )
    assert not result.success
    assert not out.exists()
    assert "keyframe" in (result.error or "").lower()
    assert result.data["keyframe_aligned"] is False
    assert result.data["nearest_keyframe"] == pytest.approx(1.6, abs=1e-3)
    assert result.data["keyframe_distance"] == pytest.approx(0.10, abs=1e-3)


@needs_ffmpeg
def test_v3_keyframe_aligned_nonzero_copy_succeeds(tmp_path: Path, dense_av: Path):
    """v3.4: keyframe-aligned nonzero copy succeeds (0.8 is a keyframe)."""
    kfs = probe_keyframe_times(dense_av)
    assert kfs is not None
    assert min(abs(t - 0.8) for t in kfs) <= KEYFRAME_ALIGN_TOLERANCE_SEC
    out = tmp_path / "aligned_nz.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(dense_av),
            "output_path": str(out),
            "start_seconds": 0.8,
            "end_seconds": 3.8,
            "codec": "copy",
        }
    )
    assert result.success, result.error
    assert out.is_file()
    assert "video" in _streams_of(out)
    assert result.data["keyframe_aligned"] is True
    assert result.data["nearest_keyframe"] == pytest.approx(0.8, abs=1e-3)
    actual = _independent_duration(out)
    assert actual is not None
    assert abs(actual - 3.0) <= copy_allowed_delta(3.0)


@needs_ffmpeg
def test_v3_start_zero_copy_succeeds(tmp_path: Path, dense_av: Path):
    """v3.5: start=0 copy succeeds when other validation passes."""
    out = tmp_path / "start0.mp4"
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
    assert result.data["keyframe_aligned"] is True


@needs_ffmpeg
def test_v3_short_window_0_8_with_1_275_output_cannot_pass(
    tmp_path: Path, dense_av: Path, monkeypatch
):
    """v3.6: 0.8 s requested with ~1.275 s output must fail.

    Covers both the mocked acceptance figure (delta 0.475 s, 59 %) and a
    real short-window copy (dense 0-0.8 overshoots to ~1.03 s).
    """
    import tools.video.video_trimmer as vt_mod

    # Unit-level: the gate itself rejects the acceptance figure.
    assert copy_allowed_delta(0.8) == pytest.approx(0.08)
    assert 0.475 > copy_allowed_delta(0.8)

    # Mocked-duration path: force the exact 1.275 s figure.
    real_dur = vt_mod.probe_output_duration
    monkeypatch.setattr(vt_mod, "probe_output_duration", lambda p: 1.275)
    try:
        out = tmp_path / "short_mock.mp4"
        result = VideoTrimmer().execute(
            {
                "operation": "cut",
                "input_path": str(dense_av),
                "output_path": str(out),
                "start_seconds": 0,
                "end_seconds": 0.8,
                "codec": "copy",
            }
        )
    finally:
        monkeypatch.setattr(vt_mod, "probe_output_duration", real_dur)
    assert not result.success
    assert not out.exists()
    assert result.data["duration_delta"] == pytest.approx(0.475, abs=1e-3)
    assert "libx264" in (result.error or "").lower()

    # Real path: dense 0-0.8 copy overshoots and must fail the tight gate.
    out2 = tmp_path / "short_real.mp4"
    result2 = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(dense_av),
            "output_path": str(out2),
            "start_seconds": 0,
            "end_seconds": 0.8,
            "codec": "copy",
        }
    )
    assert not result2.success
    assert not out2.exists()


@needs_ffmpeg
def test_v3_normal_3s_dense_copy_supported(tmp_path: Path, dense_av: Path):
    """v3.7: normal ~3 s dense copy remains supported within tolerance."""
    out = tmp_path / "normal3s.mp4"
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
    actual = _independent_duration(out)
    assert actual is not None
    assert abs(actual - 3.0) <= copy_allowed_delta(3.0)


@needs_ffmpeg
def test_v3_reencode_arbitrary_position_frame_accurate(
    tmp_path: Path, sparse_av: Path
):
    """v3.8: re-encode arbitrary-position trim remains frame-accurate."""
    out = tmp_path / "reenc_arb.mp4"
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
    assert abs(actual - 2.6) <= 0.3


@needs_ffmpeg
def test_v3_known_video_output_without_video_fails(
    tmp_path: Path, dense_av: Path, monkeypatch
):
    """v3.9: known_video -> output without video fails with truthful cleanup."""
    import tools.video.video_trimmer as vt_mod

    real_probe = vt_mod.probe_codec_types

    def fake_probe(path: Path):
        if path.name == "v_novideo.mp4":
            return {"audio"}
        return real_probe(path)

    monkeypatch.setattr(vt_mod, "probe_codec_types", fake_probe)
    out = tmp_path / "v_novideo.mp4"
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
    assert not out.exists()
    assert "no video stream" in (result.error or "")
    assert result.data["cleanup_removed"] is True
    assert result.data["output_exists_after_cleanup"] is False


@needs_ffmpeg
def test_v3_known_audio_only_output_with_audio_succeeds(
    tmp_path: Path, audio_only: Path
):
    """v3.10: known_audio_only -> output with audio succeeds."""
    out = tmp_path / "a_ok.m4a"
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
    assert "audio" in _streams_of(out)
    assert result.data["input_probe"] == "known_audio_only"
    assert result.data["output_has_audio"] is True


@needs_ffmpeg
def test_v3_known_audio_only_empty_stream_fails(
    tmp_path: Path, audio_only: Path, monkeypatch
):
    """v3.11 (failure C): audio-only + empty output stream set must fail."""
    import tools.video.video_trimmer as vt_mod

    real_probe = vt_mod.probe_codec_types

    def fake_probe(path: Path):
        if path.name == "a_empty.m4a":
            return set()
        return real_probe(path)

    monkeypatch.setattr(vt_mod, "probe_codec_types", fake_probe)
    out = tmp_path / "a_empty.m4a"
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
    assert not result.success, "empty stream set claimed audio success"
    assert not out.exists()
    err = (result.error or "").lower()
    assert "empty" in err
    assert "fail-closed" in err


@needs_ffmpeg
def test_v3_unknown_input_probe_fails_closed(
    tmp_path: Path, dense_av: Path, monkeypatch
):
    """v3.12: unknown input probe fails closed before FFmpeg."""
    import tools.video.video_trimmer as vt_mod

    real_probe = vt_mod.probe_codec_types

    def fake_probe(path: Path):
        if path.name == "unk12.mp4":
            return None
        return real_probe(path)

    monkeypatch.setattr(vt_mod, "probe_codec_types", fake_probe)
    src = tmp_path / "unk12.mp4"
    shutil.copy(dense_av, src)
    out = tmp_path / "unk12_out.mp4"
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
    assert not result.success
    assert not out.exists()
    assert result.data["input_probe"] == "unknown"


@needs_ffmpeg
def test_v3_output_probe_unknown_fails_closed(tmp_path: Path, dense_av: Path, monkeypatch):
    """v3.13: output probe unknown fails closed but leaves file."""
    import tools.video.video_trimmer as vt_mod

    real_probe = vt_mod.probe_codec_types

    def fake_probe(path: Path):
        if path.name == "v13_out.mp4":
            # Input is dense_av (real probe); only the output is unknown.
            # Distinguish by full path: output lives as v13_out.mp4.
            if str(path).endswith("v13_out.mp4") and path != dense_av:
                # Heuristic: if the file exists and is the output, hide it.
                # The input path never equals this name in this test.
                return None
        return real_probe(path)

    monkeypatch.setattr(vt_mod, "probe_codec_types", fake_probe)
    out = tmp_path / "v13_out.mp4"
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
    assert out.exists()
    assert result.data["output_probe"] == "unknown"


@needs_ffmpeg
def test_v3_cleanup_failure_reported_truthfully(
    tmp_path: Path, dense_av: Path, monkeypatch
):
    """v3.14: failed unlink must not be reported as 'output removed'."""
    import tools.video.video_trimmer as vt_mod

    monkeypatch.setattr(
        vt_mod, "_try_remove_artifact", lambda p: (False, True)
    )
    out = tmp_path / "cleanfail.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(dense_av),
            "output_path": str(out),
            "start_seconds": 0,
            "end_seconds": 0.8,
            "codec": "copy",
        }
    )
    assert not result.success
    err = (result.error or "").lower()
    assert "was removed" not in err, "must not claim removal when unlink failed"
    assert "failed" in err
    assert result.data["cleanup_removed"] is False
    assert result.data["output_exists_after_cleanup"] is True
    # Cleanup failure must never flip media failure into success.
    assert result.success is False


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
