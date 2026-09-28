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


# ------------------------------------------------------------------
# Concat segment-trim repair: one truthful trimming authority.
#
# Every concat segment carrying start_seconds/end_seconds is trimmed by
# delegating to the hardened _cut contract (interval validation,
# fail-closed probe, video keyframe-alignment gate, bounded
# copy-duration semantics, required output streams). No independent
# stream-copy path remains in _concat; a failed trim aborts the concat
# before the final join with its segment index and trim context.
# ------------------------------------------------------------------


def _concat_tmp_dir(out: Path) -> Path:
    return out.parent / ".concat_tmp"


@needs_ffmpeg
def test_concat_untrimmed_still_succeeds(tmp_path: Path, dense_av: Path):
    """Repair point 1: ordinary untrimmed concat retains existing behavior."""
    out = tmp_path / "plain_join.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(dense_av)}, {"input_path": str(dense_av)}],
        }
    )
    assert result.success, result.error
    assert out.is_file()
    streams = _streams_of(out)
    assert "video" in streams and "audio" in streams
    assert result.data["segment_count"] == 2
    assert not _concat_tmp_dir(out).exists()


@needs_ffmpeg
def test_concat_keyframe_aligned_trimmed_segment_succeeds(
    tmp_path: Path, dense_av: Path
):
    """Repair point 2: keyframe-aligned trimmed video segment succeeds."""
    out = tmp_path / "aligned_join.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(dense_av),
                    "start_seconds": 0,
                    "end_seconds": 3,
                },
                {"input_path": str(dense_av)},
            ],
        }
    )
    assert result.success, result.error
    assert out.is_file()
    streams = _streams_of(out)
    assert "video" in streams and "audio" in streams
    assert not _concat_tmp_dir(out).exists()


@needs_ffmpeg
def test_concat_sparse_nonkeyframe_trim_fails_not_false_success(
    tmp_path: Path, sparse_av: Path, dense_av: Path
):
    """Repair point 3: sparse/non-keyframe trim fails, never false-succeeds.

    Pre-fix this exact concat reported success while the final output had
    lost its video stream (audio-only) — the B1 contract bypass.
    """
    out = tmp_path / "sparse_join.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(sparse_av),
                    "start_seconds": 1.7,
                    "end_seconds": 4.3,
                },
                {"input_path": str(dense_av)},
            ],
        }
    )
    assert not result.success
    err = (result.error or "").lower()
    assert "re-encode" in err and "libx264" in err
    assert not out.exists(), "no partial final artifact may be presented"
    assert result.data["failed_segment_index"] == 0


@needs_ffmpeg
def test_concat_zero_length_segment_fails(tmp_path: Path, dense_av: Path):
    """Repair point 4: end == start fails via the cut interval contract."""
    out = tmp_path / "zero_join.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(dense_av),
                    "start_seconds": 2,
                    "end_seconds": 2,
                }
            ],
        }
    )
    assert not result.success
    err = (result.error or "").lower()
    assert "end_seconds" in err and "greater than" in err
    assert "no ffmpeg operation was executed" in err
    assert not out.exists()
    assert result.data["failed_segment_index"] == 0


@needs_ffmpeg
def test_concat_negative_length_segment_fails(tmp_path: Path, dense_av: Path):
    """Repair point 5: end < start fails via the cut interval contract."""
    out = tmp_path / "neg_join.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(dense_av),
                    "start_seconds": 3,
                    "end_seconds": 2,
                }
            ],
        }
    )
    assert not result.success
    assert "greater than" in (result.error or "").lower()
    assert result.data["trim_data"]["requested_duration"] == pytest.approx(-1.0)
    assert not out.exists()
    assert result.data["failed_segment_index"] == 0


@needs_ffmpeg
def test_concat_short_window_cannot_bypass_timing(
    tmp_path: Path, dense_av: Path
):
    """Repair point 6: short-window copy segment obeys B1 timing semantics.

    Dense 0-0.8 copy overshoots to ~1.03 s which exceeds the tight
    min(0.30, 0.8*0.10)=0.08 s gate, so the segment — and the concat —
    must fail rather than smuggle an overlong window into the join.
    """
    out = tmp_path / "short_join.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(dense_av),
                    "start_seconds": 0,
                    "end_seconds": 0.8,
                }
            ],
        }
    )
    assert not result.success
    assert "libx264" in (result.error or "").lower()
    assert not out.exists()
    assert result.data["failed_segment_index"] == 0


@needs_ffmpeg
def test_concat_video_segment_without_proven_video_cannot_join(
    tmp_path: Path, dense_av: Path, monkeypatch
):
    """Repair point 7: known-video segment needs proven video output.

    Forces the _cut output probe to audio-only (the old silent video-drop
    condition). The trim must fail and the failed segment must never enter
    the concat list — the final FFmpeg join must never execute.
    """
    import tools.video.video_trimmer as vt_mod

    real_probe = vt_mod.probe_codec_types

    def fake_probe(path: Path):
        if path.name.startswith("seg_"):
            return {"audio"}  # simulated dropped-video trim output
        return real_probe(path)

    monkeypatch.setattr(vt_mod, "probe_codec_types", fake_probe)

    real_run = VideoTrimmer.run_command
    calls: list[list[str]] = []

    def spy_run(self, cmd: list[str], **kwargs):
        calls.append(list(cmd))
        return real_run(self, cmd, **kwargs)

    monkeypatch.setattr(VideoTrimmer, "run_command", spy_run)

    out = tmp_path / "novideo_join.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(dense_av),
                    "start_seconds": 0,
                    "end_seconds": 2,
                }
            ],
        }
    )
    assert not result.success
    assert "no video stream" in (result.error or "")
    assert not out.exists()
    assert result.data["failed_segment_index"] == 0
    assert not any("concat" in c for c in calls), (
        "failed trim must never reach the final join"
    )


@needs_ffmpeg
def test_concat_audio_only_trimmed_segment_supported(
    tmp_path: Path, audio_only: Path
):
    """Repair point 8: known-audio-only trimmed segment remains supported."""
    out = tmp_path / "audio_join.m4a"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(audio_only),
                    "start_seconds": 0,
                    "end_seconds": 2,
                },
                {"input_path": str(audio_only)},
            ],
        }
    )
    assert result.success, result.error
    assert out.is_file()
    streams = _streams_of(out)
    assert "audio" in streams
    assert "video" not in streams
    actual = _independent_duration(out)
    assert actual is not None
    assert abs(actual - 6.0) <= 0.6


@needs_ffmpeg
def test_concat_unknown_probe_segment_fails_closed(
    tmp_path: Path, dense_av: Path, monkeypatch
):
    """Repair point 9: unknown segment input probe fails closed."""
    import tools.video.video_trimmer as vt_mod

    real_probe = vt_mod.probe_codec_types

    def fake_probe(path: Path):
        if path.name == "mystery_seg.mp4":
            return None
        return real_probe(path)

    monkeypatch.setattr(vt_mod, "probe_codec_types", fake_probe)

    src = tmp_path / "mystery_seg.mp4"
    shutil.copy(dense_av, src)
    out = tmp_path / "mystery_join.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(src),
                    "start_seconds": 0,
                    "end_seconds": 2,
                }
            ],
        }
    )
    assert not result.success
    assert "unknown" in (result.error or "").lower()
    assert not out.exists()
    assert result.data["failed_segment_index"] == 0
    assert result.data["trim_data"]["input_probe"] == "unknown"


@needs_ffmpeg
def test_concat_failing_segment_reports_index_and_context(
    tmp_path: Path, dense_av: Path, sparse_av: Path
):
    """Repair point 10: the failing segment index and trim context survive."""
    out = tmp_path / "indexed_join.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(dense_av),
                    "start_seconds": 0,
                    "end_seconds": 3,
                },
                {
                    "input_path": str(sparse_av),
                    "start_seconds": 1.7,
                    "end_seconds": 4.3,
                },
            ],
        }
    )
    assert not result.success
    assert result.data["failed_segment_index"] == 1
    assert result.data["segment_input"] == str(sparse_av)
    assert "segment 1" in (result.error or "")
    assert result.data["trim_error"], "underlying trim error must be preserved"
    assert "keyframe" in (result.data["trim_error"] or "").lower()
    assert result.data["trim_data"]["keyframe_aligned"] is False
    assert not out.exists()


@needs_ffmpeg
def test_concat_later_segments_not_executed_after_trim_failure(
    tmp_path: Path, sparse_av: Path, dense_av: Path, monkeypatch
):
    """Repair point 11: nothing after an earlier trim failure executes.

    Segment 0 fails pre-FFmpeg (keyframe gate); segment 1 is a valid
    trimmed segment that would run FFmpeg if the loop continued, and the
    final join would also run FFmpeg. Zero FFmpeg calls must be observed.
    """
    real_run = VideoTrimmer.run_command
    calls: list[list[str]] = []

    def spy_run(self, cmd: list[str], **kwargs):
        calls.append(list(cmd))
        return real_run(self, cmd, **kwargs)

    monkeypatch.setattr(VideoTrimmer, "run_command", spy_run)

    out = tmp_path / "halted_join.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(sparse_av),
                    "start_seconds": 1.7,
                    "end_seconds": 4.3,
                },
                {
                    "input_path": str(dense_av),
                    "start_seconds": 0,
                    "end_seconds": 2,
                },
            ],
        }
    )
    assert not result.success
    assert result.data["failed_segment_index"] == 0
    assert calls == [], f"no FFmpeg may run after trim failure, saw: {calls}"
    assert not out.exists()


@needs_ffmpeg
def test_concat_temp_artifacts_cleaned_on_failure(
    tmp_path: Path, dense_av: Path, sparse_av: Path
):
    """Repair point 12: failure cleans temp artifacts without touching inputs."""
    out = tmp_path / "messy_join.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(dense_av),
                    "start_seconds": 0,
                    "end_seconds": 3,
                },
                {
                    "input_path": str(sparse_av),
                    "start_seconds": 1.7,
                    "end_seconds": 4.3,
                },
            ],
        }
    )
    assert not result.success
    # First segment's valid temp trim must also be cleaned on abort.
    assert list(tmp_path.rglob("seg_*.mp4")) == []
    assert list(tmp_path.rglob("concat_list.txt")) == []
    assert not _concat_tmp_dir(out).exists()
    assert result.data["cleanup_attempted"] is True
    assert result.data["temp_dir_exists_after"] is False
    assert not out.exists()
    # Original inputs are never deleted.
    assert dense_av.is_file() and sparse_av.is_file()


@needs_ffmpeg
def test_concat_trimmed_multi_segment_final_media_valid(
    tmp_path: Path, dense_av: Path
):
    """Repair point 13: successful trimmed multi-segment concat is valid."""
    out = tmp_path / "multi_join.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(dense_av),
                    "start_seconds": 0,
                    "end_seconds": 3,
                },
                {
                    "input_path": str(dense_av),
                    "start_seconds": 0.8,
                    "end_seconds": 3.8,
                },
            ],
        }
    )
    assert result.success, result.error
    assert out.is_file()
    streams = _streams_of(out)
    assert "video" in streams and "audio" in streams
    actual = _independent_duration(out)
    assert actual is not None
    assert abs(actual - 6.0) <= 0.6, f"joined duration drifted: {actual}s vs 6.0s"
    assert not _concat_tmp_dir(out).exists()


@needs_ffmpeg
def test_cut_behavior_unchanged_by_concat_repair(
    tmp_path: Path, dense_av: Path, sparse_av: Path
):
    """Repair point 14: existing cut behavior is unchanged."""
    ok_out = tmp_path / "cut_still_ok.mp4"
    ok_result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(dense_av),
            "output_path": str(ok_out),
            "start_seconds": 0,
            "end_seconds": 3,
            "codec": "copy",
        }
    )
    assert ok_result.success, ok_result.error
    assert "video" in _streams_of(ok_out)
    assert ok_result.data["keyframe_aligned"] is True

    bad_out = tmp_path / "cut_still_rejects.mp4"
    bad_result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(sparse_av),
            "output_path": str(bad_out),
            "start_seconds": 1.7,
            "end_seconds": 4.3,
            "codec": "copy",
        }
    )
    assert not bad_result.success
    assert not bad_out.exists()
    assert bad_result.data["keyframe_aligned"] is False


@needs_ffmpeg
def test_speed_behavior_unchanged_by_concat_repair(
    tmp_path: Path, dense_av: Path
):
    """Repair point 15: speed behavior is unchanged."""
    out = tmp_path / "still_fast.mp4"
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


# ------------------------------------------------------------------
# Concat cleanup corrective repair v2: failure-safe cleanup ownership,
# narrow segment-exception semantics, non-masking cleanup.
#
# Every temp path is cleanup-owned before _cut; _cut exceptions preserve
# the segment index; cleanup can never mask the primary trim/join error.
# ------------------------------------------------------------------


def test_concat_cleanup_v2_failed_trim_artifact_removed(tmp_path: Path):
    """v2.1: failed _cut that leaves an artifact is still cleaned."""
    from tools.base_tool import ToolResult

    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"
    tmpdir = out.parent / ".concat_tmp"

    def fake_cut(self, inputs):
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"leftover-for-inspection")
        return ToolResult(
            success=False,
            error="Cut output could not be stream-validated (simulated); left for inspection.",
            data={"operation": "cut", "output_probe": "unknown"},
        )

    import unittest.mock as mock

    with mock.patch.object(VideoTrimmer, "_cut", fake_cut):
        result = VideoTrimmer().execute(
            {
                "operation": "concat",
                "output_path": str(out),
                "segments": [
                    {"input_path": str(a), "start_seconds": 0, "end_seconds": 2}
                ],
            }
        )
    assert not result.success
    assert result.data["failed_segment_index"] == 0
    assert "simulated" in (result.data["trim_error"] or "")
    assert list(tmp_path.rglob("seg_*.mp4")) == []
    assert not tmpdir.exists()
    assert result.data["temp_dir_exists_after"] is False
    assert a.is_file(), "original inputs must never be deleted"


def test_concat_cleanup_v2_failed_trim_no_artifact_harmless(tmp_path: Path):
    """v2.2: failed _cut with no artifact leaves cleanup harmless."""
    from tools.base_tool import ToolResult

    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"
    tmpdir = out.parent / ".concat_tmp"

    def fake_cut(self, inputs):
        return ToolResult(
            success=False,
            error="Invalid cut interval (simulated); no artifact created.",
            data={"operation": "cut"},
        )

    import unittest.mock as mock

    with mock.patch.object(VideoTrimmer, "_cut", fake_cut):
        result = VideoTrimmer().execute(
            {
                "operation": "concat",
                "output_path": str(out),
                "segments": [
                    {"input_path": str(a), "start_seconds": 3, "end_seconds": 2}
                ],
            }
        )
    assert not result.success
    assert result.data["failed_segment_index"] == 0
    assert result.data["cleanup_attempted"] is True
    assert not tmpdir.exists()
    assert result.data["temp_dir_exists_after"] is False
    assert a.is_file()


def test_concat_cleanup_v2_cut_raises_segment0(tmp_path: Path, monkeypatch):
    """v2.3: _cut raising on segment 0 preserves index 0."""
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"fake-a")
    b.write_bytes(b"fake-b")
    out = tmp_path / "out.mp4"

    def boom(self, inputs):
        raise RuntimeError("simulated FFmpeg failure: invalid explicit codec")

    monkeypatch.setattr(VideoTrimmer, "_cut", boom)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {"input_path": str(a), "start_seconds": 0, "end_seconds": 2},
                {"input_path": str(b), "start_seconds": 0, "end_seconds": 2},
            ],
        }
    )
    assert not result.success
    assert result.data["failed_segment_index"] == 0
    assert result.data["segment_input"] == str(a)
    assert result.data["final_output_created"] is False
    assert "invalid explicit codec" in (result.error or "").lower()
    assert "RuntimeError" in (result.error or "")
    assert result.data["trim_exception_type"] == "RuntimeError"
    assert not out.exists()
    assert not (out.parent / ".concat_tmp").exists()


def test_concat_cleanup_v2_cut_raises_middle_segment(tmp_path: Path, monkeypatch):
    """v2.4: _cut raising on a middle segment preserves the right index."""
    from tools.base_tool import ToolResult

    files = []
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        p = tmp_path / name
        p.write_bytes(b"fake")
        files.append(p)
    out = tmp_path / "out.mp4"

    def fake_cut(self, inputs):
        op = inputs["output_path"]
        if "seg_0001" in op:
            raise RuntimeError("simulated FFmpeg failure on middle segment")
        p = Path(op)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"good-temp")
        return ToolResult(success=True, data={"operation": "cut"})

    monkeypatch.setattr(VideoTrimmer, "_cut", fake_cut)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {"input_path": str(files[0]), "start_seconds": 0, "end_seconds": 2},
                {"input_path": str(files[1]), "start_seconds": 0, "end_seconds": 2},
                {"input_path": str(files[2]), "start_seconds": 0, "end_seconds": 2},
            ],
        }
    )
    assert not result.success
    assert result.data["failed_segment_index"] == 1
    assert result.data["segment_input"] == str(files[1])
    assert result.data["final_output_created"] is False
    assert "middle segment" in (result.error or "")
    assert not out.exists()


def test_concat_cleanup_v2_later_segments_not_executed_after_raise(
    tmp_path: Path, monkeypatch
):
    """v2.5: segments after a thrown trim exception never execute."""
    from tools.base_tool import ToolResult

    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    c = tmp_path / "c.mp4"
    for p in (a, b, c):
        p.write_bytes(b"fake")
    out = tmp_path / "out.mp4"
    cut_calls: list[str] = []

    def fake_cut(self, inputs):
        cut_calls.append(str(inputs["output_path"]))
        if "seg_0000" in str(inputs["output_path"]):
            raise RuntimeError("boom on segment 0")
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"good")
        return ToolResult(success=True, data={"operation": "cut"})

    monkeypatch.setattr(VideoTrimmer, "_cut", fake_cut)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {"input_path": str(a), "start_seconds": 0, "end_seconds": 1},
                {"input_path": str(b), "start_seconds": 0, "end_seconds": 1},
                {"input_path": str(c), "start_seconds": 0, "end_seconds": 1},
            ],
        }
    )
    assert not result.success
    assert result.data["failed_segment_index"] == 0
    assert len(cut_calls) == 1, f"only segment 0 may execute, saw: {cut_calls}"
    assert "seg_0000" in cut_calls[0]
    for p in (a, b, c):
        assert p.is_file()


def test_concat_cleanup_v2_join_not_executed_after_raise(
    tmp_path: Path, monkeypatch
):
    """v2.6: final join never executes after a thrown trim exception."""
    real_run = VideoTrimmer.run_command
    calls: list[list[str]] = []

    def spy_run(self, cmd: list[str], **kwargs):
        calls.append(list(cmd))
        return real_run(self, cmd, **kwargs)

    monkeypatch.setattr(VideoTrimmer, "run_command", spy_run)

    def boom(self, inputs):
        raise RuntimeError("simulated trim exception blocks join")

    monkeypatch.setattr(VideoTrimmer, "_cut", boom)
    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(a), "start_seconds": 0, "end_seconds": 2}],
        }
    )
    assert not result.success
    assert result.data["failed_segment_index"] == 0
    assert not any("concat" in c for c in calls), (
        f"failed trim must never reach the final join, saw: {calls}"
    )
    assert not out.exists()


def test_concat_cleanup_v2_unlink_failure_does_not_mask(tmp_path: Path, monkeypatch):
    """v2.7: cleanup unlink failure preserves the primary trim failure."""
    from tools.base_tool import ToolResult

    import tools.video.video_trimmer as vt_mod

    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"fake-a")
    b.write_bytes(b"fake-b")
    out = tmp_path / "out.mp4"

    def fake_cut(self, inputs):
        op = str(inputs["output_path"])
        if "seg_0000" in op:
            p = Path(inputs["output_path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"good-temp")
            return ToolResult(success=True, data={"operation": "cut"})
        return ToolResult(
            success=False,
            error="simulated sparse keyframe trim failure (primary)",
            data={"operation": "cut", "keyframe_aligned": False},
        )

    monkeypatch.setattr(VideoTrimmer, "_cut", fake_cut)
    monkeypatch.setattr(
        vt_mod, "_try_remove_artifact", lambda p: (_ for _ in ()).throw(OSError("injected unlink failure"))
    )
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {"input_path": str(a), "start_seconds": 0, "end_seconds": 2},
                {"input_path": str(b), "start_seconds": 1.7, "end_seconds": 4.3},
            ],
        }
    )
    assert not result.success
    # Primary trim failure remains authoritative, not the cleanup OSError.
    assert "sparse keyframe trim failure (primary)" in (result.error or "")
    assert "injected unlink failure" not in (result.error or "")
    assert result.data["failed_segment_index"] == 1
    assert result.data["trim_error"] is not None
    assert "sparse keyframe" in (result.data["trim_error"] or "")
    assert result.data["cleanup_attempted"] is True
    for p in (a, b):
        assert p.is_file(), "originals must survive cleanup failure"


def test_concat_cleanup_v2_rmdir_failure_does_not_mask(tmp_path: Path, monkeypatch):
    """v2.8: temp-dir removal failure preserves the primary trim failure."""
    from tools.base_tool import ToolResult

    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"

    def fake_cut(self, inputs):
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"good-temp")
        return ToolResult(success=True, data={"operation": "cut"})

    # First segment succeeds; second has an invalid interval (primary failure).
    # Force rmdir to fail so cleanup reports the dir as remaining.
    real_cut = fake_cut

    def routing_cut(self, inputs):
        if "seg_0001" in str(inputs["output_path"]):
            return ToolResult(
                success=False,
                error="Invalid cut interval (simulated primary); no artifact.",
                data={"operation": "cut"},
            )
        return real_cut(self, inputs)

    monkeypatch.setattr(VideoTrimmer, "_cut", routing_cut)
    real_rmdir = Path.rmdir

    def boom_rmdir(self):
        raise OSError("injected rmdir failure")

    monkeypatch.setattr(Path, "rmdir", boom_rmdir)
    b = tmp_path / "b.mp4"
    b.write_bytes(b"fake-b")
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {"input_path": str(a), "start_seconds": 0, "end_seconds": 2},
                {"input_path": str(b), "start_seconds": 3, "end_seconds": 2},
            ],
        }
    )
    assert not result.success
    assert "simulated primary" in (result.error or "")
    assert "injected rmdir failure" not in (result.error or "")
    assert result.data["failed_segment_index"] == 1
    assert result.data["temp_dir_exists_after"] is True
    assert result.data["cleanup_ok"] is False
    assert a.is_file() and b.is_file()


def test_concat_cleanup_v2_cleanup_failure_reports_remaining_truthfully(
    tmp_path: Path, monkeypatch
):
    """v2.9: cleanup failure reports which paths remain without false claims."""
    from tools.base_tool import ToolResult

    import tools.video.video_trimmer as vt_mod

    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"fake-a")
    b.write_bytes(b"fake-b")
    out = tmp_path / "out.mp4"

    def fake_cut(self, inputs):
        op = str(inputs["output_path"])
        if "seg_0000" in op:
            p = Path(inputs["output_path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"good-temp")
            return ToolResult(success=True, data={"operation": "cut"})
        return ToolResult(
            success=False,
            error="simulated primary trim failure",
            data={"operation": "cut"},
        )

    monkeypatch.setattr(VideoTrimmer, "_cut", fake_cut)
    monkeypatch.setattr(
        vt_mod, "_try_remove_artifact", lambda p: (False, True)
    )
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {"input_path": str(a), "start_seconds": 0, "end_seconds": 2},
                {"input_path": str(b), "start_seconds": 0, "end_seconds": 2},
            ],
        }
    )
    assert not result.success
    assert "simulated primary trim failure" in (result.error or "")
    details = result.data["temp_file_details"]
    assert len(details) >= 1
    assert any(d["exists_after"] is True for d in details)
    assert any(d["removed"] is False for d in details)
    remaining = result.data["cleanup_remaining_paths"]
    assert any("seg_0000" in p for p in remaining), (
        f"remaining paths must name the unremoved temp, saw: {remaining}"
    )
    assert result.data["cleanup_ok"] is False
    assert "was removed" not in (result.error or "").lower()


def test_concat_cleanup_v2_join_exception_preserved_with_clean_cleanup(
    tmp_path: Path, monkeypatch
):
    """v2.10: final-join exception + clean cleanup preserves the join error."""
    from tools.base_tool import ToolResult
    from tools.base_tool import ToolCommandError

    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"

    def fake_cut(self, inputs):
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"good-temp")
        return ToolResult(success=True, data={"operation": "cut"})

    monkeypatch.setattr(VideoTrimmer, "_cut", fake_cut)

    def boom_join(self, cmd: list[str], **kwargs):
        raise ToolCommandError(1, cmd, detail="simulated join codec failure")

    monkeypatch.setattr(VideoTrimmer, "run_command", boom_join)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(a), "start_seconds": 0, "end_seconds": 2}],
        }
    )
    assert not result.success
    assert "concat join failed" in (result.error or "")
    assert "simulated join codec failure" in (result.error or "")
    assert result.data["final_output_created"] is False
    assert result.data["cleanup_attempted"] is True
    assert result.data["temp_dir_exists_after"] is False
    assert list(tmp_path.rglob("seg_*.mp4")) == []
    assert a.is_file()


def test_concat_cleanup_v2_join_exception_plus_cleanup_failure_preserves_join(
    tmp_path: Path, monkeypatch
):
    """v2.11: join exception + cleanup exception still preserves join error."""
    from tools.base_tool import ToolResult
    from tools.base_tool import ToolCommandError

    import tools.video.video_trimmer as vt_mod

    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"

    def fake_cut(self, inputs):
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"good-temp")
        return ToolResult(success=True, data={"operation": "cut"})

    monkeypatch.setattr(VideoTrimmer, "_cut", fake_cut)

    def boom_join(self, cmd: list[str], **kwargs):
        raise ToolCommandError(1, cmd, detail="simulated primary join failure")

    monkeypatch.setattr(VideoTrimmer, "run_command", boom_join)
    monkeypatch.setattr(
        vt_mod, "_try_remove_artifact", lambda p: (_ for _ in ()).throw(OSError("injected cleanup OSError"))
    )
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(a), "start_seconds": 0, "end_seconds": 2}],
        }
    )
    assert not result.success
    assert "simulated primary join failure" in (result.error or "")
    assert "injected cleanup OSError" not in (result.error or "")
    assert result.data["final_output_created"] is False
    assert result.data["cleanup_attempted"] is True
    assert a.is_file(), "original must survive join+cleanup failures"


def test_concat_cleanup_v2_originals_never_deleted(tmp_path: Path, monkeypatch):
    """v2.12: original inputs survive trim failure, raise, and join failure."""
    from tools.base_tool import ToolResult
    from tools.base_tool import ToolCommandError

    a = tmp_path / "orig_a.mp4"
    b = tmp_path / "orig_b.mp4"
    a.write_bytes(b"orig-a")
    b.write_bytes(b"orig-b")

    # Case 1: trim failure (no artifact).
    def fail_cut(self, inputs):
        return ToolResult(success=False, error="simulated primary failure", data={})

    monkeypatch.setattr(VideoTrimmer, "_cut", fail_cut)
    out1 = tmp_path / "o1.mp4"
    r1 = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out1),
            "segments": [{"input_path": str(a), "start_seconds": 0, "end_seconds": 1}],
        }
    )
    assert not r1.success
    assert a.is_file() and b.is_file()

    # Case 2: trim raises.
    def boom_cut(self, inputs):
        raise RuntimeError("simulated raise")

    monkeypatch.setattr(VideoTrimmer, "_cut", boom_cut)
    out2 = tmp_path / "o2.mp4"
    r2 = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out2),
            "segments": [{"input_path": str(a), "start_seconds": 0, "end_seconds": 1}],
        }
    )
    assert not r2.success
    assert a.is_file() and b.is_file()

    # Case 3: join failure after successful trims.
    def ok_cut(self, inputs):
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"good")
        return ToolResult(success=True, data={})

    monkeypatch.setattr(VideoTrimmer, "_cut", ok_cut)

    def boom_join(self, cmd: list[str], **kwargs):
        raise ToolCommandError(1, cmd, detail="join boom")

    monkeypatch.setattr(VideoTrimmer, "run_command", boom_join)
    out3 = tmp_path / "o3.mp4"
    r3 = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out3),
            "segments": [{"input_path": str(a)}, {"input_path": str(b)}],
        }
    )
    assert not r3.success
    assert a.is_file() and b.is_file()


@needs_ffmpeg
def test_concat_cleanup_v2_success_cleanup_unchanged(tmp_path: Path, dense_av: Path):
    """v2.13: successful concat cleanup semantics unchanged (real FFmpeg)."""
    out = tmp_path / "v2_ok.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(dense_av)}, {"input_path": str(dense_av)}],
        }
    )
    assert result.success, result.error
    assert out.is_file()
    assert "video" in _streams_of(out)
    assert result.data["cleanup_attempted"] is True
    assert result.data["temp_dir_exists_after"] is False
    assert result.data["cleanup_ok"] is True
    assert result.data["cleanup_remaining_paths"] == []
    assert not (out.parent / ".concat_tmp").exists()


@needs_ffmpeg
def test_concat_cleanup_v2_aligned_trimmed_unchanged(tmp_path: Path, dense_av: Path):
    """v2.14: aligned trimmed concat still succeeds (real FFmpeg)."""
    out = tmp_path / "v2_aligned.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(dense_av),
                    "start_seconds": 0,
                    "end_seconds": 3,
                },
                {"input_path": str(dense_av)},
            ],
        }
    )
    assert result.success, result.error
    assert out.is_file()
    assert "video" in _streams_of(out)
    assert not (out.parent / ".concat_tmp").exists()


@needs_ffmpeg
def test_concat_cleanup_v2_sparse_failure_unchanged(
    tmp_path: Path, sparse_av: Path, dense_av: Path
):
    """v2.15: sparse/non-keyframe failure semantics unchanged (real FFmpeg)."""
    out = tmp_path / "v2_sparse.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(sparse_av),
                    "start_seconds": 1.7,
                    "end_seconds": 4.3,
                },
                {"input_path": str(dense_av)},
            ],
        }
    )
    assert not result.success
    err = (result.error or "").lower()
    assert "re-encode" in err and "libx264" in err
    assert result.data["failed_segment_index"] == 0
    assert result.data["final_output_created"] is False
    assert not out.exists()
    assert not (out.parent / ".concat_tmp").exists()
    assert sparse_av.is_file() and dense_av.is_file()


@needs_ffmpeg
def test_concat_cleanup_v2_explicit_libx264_unchanged(
    tmp_path: Path, sparse_av: Path, dense_av: Path
):
    """v2.16: explicit codec='libx264' re-encode trim still succeeds in concat."""
    out = tmp_path / "v2_libx264.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "codec": "libx264",
            "segments": [
                {
                    "input_path": str(sparse_av),
                    "start_seconds": 1.7,
                    "end_seconds": 4.3,
                },
                {
                    "input_path": str(dense_av),
                    "start_seconds": 0,
                    "end_seconds": 2,
                },
            ],
        }
    )
    assert result.success, result.error
    assert out.is_file()
    assert "video" in _streams_of(out)
    assert not (out.parent / ".concat_tmp").exists()


@needs_ffmpeg
def test_concat_cleanup_v2_cut_production_unchanged(
    tmp_path: Path, dense_av: Path, sparse_av: Path
):
    """v2.17: _cut production behavior unchanged by the concat repair."""
    ok_out = tmp_path / "v2_cut_ok.mp4"
    ok_result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(dense_av),
            "output_path": str(ok_out),
            "start_seconds": 0,
            "end_seconds": 3,
            "codec": "copy",
        }
    )
    assert ok_result.success, ok_result.error
    assert "video" in _streams_of(ok_out)
    assert ok_result.data["keyframe_aligned"] is True

    bad_out = tmp_path / "v2_cut_bad.mp4"
    bad_result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(sparse_av),
            "output_path": str(bad_out),
            "start_seconds": 1.7,
            "end_seconds": 4.3,
            "codec": "copy",
        }
    )
    assert not bad_result.success
    assert not bad_out.exists()
    assert bad_result.data["keyframe_aligned"] is False


@needs_ffmpeg
def test_concat_cleanup_v2_speed_unchanged(tmp_path: Path, dense_av: Path):
    """v2.18: _speed unchanged by the concat repair."""
    out = tmp_path / "v2_fast.mp4"
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


# ------------------------------------------------------------------
# v3: explicit concat temp ownership (O1), malformed-segment authority
# (O2), cleanup exception-text diagnostics (O3), partial final output
# handling (O4). _cut/_speed semantics must be unchanged.
# ------------------------------------------------------------------


def test_concat_ownership_v3_original_in_tempdir_never_deleted(tmp_path: Path):
    """v3.1 (O1): an original input inside .concat_tmp is never deleted."""
    from tools.base_tool import ToolResult

    tmpdir = tmp_path / ".concat_tmp"
    tmpdir.mkdir(parents=True)
    orig = tmpdir / "seg_0000.mp4"  # collision-prone name, IS the input
    orig.write_bytes(b"ORIGINAL-BYTES-MUST-SURVIVE")
    b = tmp_path / "b.mp4"
    b.write_bytes(b"fake-b")
    out = tmp_path / "out.mp4"

    def fail_cut(self, inputs):
        return ToolResult(success=False, error="simulated trim failure", data={})

    import unittest.mock as mock

    with mock.patch.object(VideoTrimmer, "_cut", fail_cut):
        result = VideoTrimmer().execute(
            {
                "operation": "concat",
                "output_path": str(out),
                "segments": [
                    {"input_path": str(orig), "start_seconds": 0, "end_seconds": 1},
                    {"input_path": str(b), "start_seconds": 0, "end_seconds": 1},
                ],
            }
        )
    assert not result.success
    assert result.data["failed_segment_index"] == 0
    assert orig.is_file(), "original input must never be deleted by cleanup"
    assert orig.read_bytes() == b"ORIGINAL-BYTES-MUST-SURVIVE"
    owned = [d["path"] for d in result.data["temp_file_details"]]
    assert str(orig) not in owned, "original must never be cleanup-owned"
    assert b.is_file()


def test_concat_ownership_v3_collision_resolves_safely(tmp_path: Path):
    """v3.2 (O1): original/temp filename collision resolves safely."""
    from tools.base_tool import ToolResult

    tmpdir = tmp_path / ".concat_tmp"
    tmpdir.mkdir(parents=True)
    orig = tmpdir / "seg_0000.mp4"
    orig.write_bytes(b"ORIGINAL-BYTES-MUST-SURVIVE")
    out = tmp_path / "out.mp4"
    cut_outputs: list[str] = []

    def ok_cut(self, inputs):
        cut_outputs.append(str(inputs["output_path"]))
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"good-temp")
        return ToolResult(success=True, data={"operation": "cut"})

    import unittest.mock as mock

    with mock.patch.object(VideoTrimmer, "_cut", ok_cut), mock.patch.object(
        VideoTrimmer, "run_command", lambda self, cmd, **kw: None
    ):
        result = VideoTrimmer().execute(
            {
                "operation": "concat",
                "output_path": str(out),
                "segments": [
                    {"input_path": str(orig), "start_seconds": 0, "end_seconds": 1}
                ],
            }
        )
    assert result.success, result.error
    assert len(cut_outputs) == 1
    assert cut_outputs[0] != str(orig), "temp must not reuse the original path"
    assert "seg_0000" in cut_outputs[0], "historical temp shape preserved"
    assert orig.is_file()
    assert orig.read_bytes() == b"ORIGINAL-BYTES-MUST-SURVIVE"
    # Only the original matches the glob; the reserved temp is gone.
    assert [p for p in tmp_path.rglob("seg_*.mp4")] == [orig]
    assert sorted(tmpdir.iterdir()) == [orig]


def test_concat_ownership_v3_malformed_empty_dict_structured(tmp_path: Path):
    """v3.3 (O2): segments=[{}] returns structured failure, no leak."""
    out = tmp_path / "out.mp4"
    result = VideoTrimmer().execute(
        {"operation": "concat", "output_path": str(out), "segments": [{}]}
    )
    assert not result.success
    assert "malformed" in (result.error or "").lower()
    assert result.data["failed_segment_index"] == 0
    assert result.data["malformed_segment"] is True
    assert result.data["final_output_created"] is False
    assert result.data["cleanup_attempted"] is True
    assert result.data["temp_dir_exists_after"] is False
    assert result.data["cleanup_remaining_paths"] == []
    assert not (tmp_path / ".concat_tmp").exists()
    assert not out.exists()


def test_concat_ownership_v3_malformed_nondict_structured(tmp_path: Path):
    """v3.4 (O2): non-dict segments return structured failure."""
    out = tmp_path / "out.mp4"
    for bad in ("not-a-dict", None, 123, ["input_path"]):
        result = VideoTrimmer().execute(
            {
                "operation": "concat",
                "output_path": str(out),
                "segments": [bad],
            }
        )
        assert not result.success, f"segment {bad!r} must fail"
        assert result.data["failed_segment_index"] == 0
        assert result.data["malformed_segment"] is True
        assert result.data["final_output_created"] is False
    assert not (tmp_path / ".concat_tmp").exists()


def test_concat_ownership_v3_malformed_first_leaves_no_tempdir(tmp_path: Path):
    """v3.5 (O2+lifecycle): malformed first segment leaves no temp dir."""
    out = tmp_path / "out.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"no_input": 1}],
        }
    )
    assert not result.success
    assert "missing" in (result.data["segment_error"] or "").lower()
    assert result.data["failed_segment_index"] == 0
    assert not (tmp_path / ".concat_tmp").exists()
    assert result.data["temp_dir_exists_after"] is False
    assert result.data["cleanup_ok"] is True


def test_concat_ownership_v3_malformed_middle_cleans_owned_temps(
    tmp_path: Path, monkeypatch
):
    """v3.6 (O2): malformed middle segment cleans earlier owned temps."""
    from tools.base_tool import ToolResult

    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"fake-a")
    b.write_bytes(b"fake-b")
    out = tmp_path / "out.mp4"

    def fake_cut(self, inputs):
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"good-temp")
        return ToolResult(success=True, data={"operation": "cut"})

    monkeypatch.setattr(VideoTrimmer, "_cut", fake_cut)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {"input_path": str(a), "start_seconds": 0, "end_seconds": 1},
                {"input_path": str(b), "start_seconds": 0, "end_seconds": 1},
                {},
            ],
        }
    )
    assert not result.success
    assert result.data["failed_segment_index"] == 2
    assert result.data["malformed_segment"] is True
    assert result.data["final_output_created"] is False
    assert list(tmp_path.rglob("seg_*.mp4")) == []
    assert not (tmp_path / ".concat_tmp").exists()
    assert a.is_file() and b.is_file()


def test_concat_ownership_v3_malformed_index_correct(tmp_path: Path):
    """v3.7 (O2): unusable input_path reports the correct segment index."""
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"fake-a")
    b.write_bytes(b"fake-b")
    out = tmp_path / "out.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {"input_path": str(a)},
                {"input_path": str(b)},
                {"input_path": None},
            ],
        }
    )
    assert not result.success
    assert result.data["failed_segment_index"] == 2
    assert "unusable" in (result.data["segment_error"] or "").lower()
    assert a.is_file() and b.is_file()
    assert not (tmp_path / ".concat_tmp").exists()


def test_concat_ownership_v3_no_join_after_malformed(tmp_path: Path, monkeypatch):
    """v3.8 (O2): the final join never runs after a malformed segment."""
    real_run = VideoTrimmer.run_command
    calls: list[list[str]] = []

    def spy_run(self, cmd: list[str], **kwargs):
        calls.append(list(cmd))
        return real_run(self, cmd, **kwargs)

    monkeypatch.setattr(VideoTrimmer, "run_command", spy_run)
    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(a)}, {}],
        }
    )
    assert not result.success
    assert result.data["failed_segment_index"] == 1
    assert not any("concat" in c for c in calls)
    assert not out.exists()


def test_concat_ownership_v3_unlink_oserror_preserves_text(
    tmp_path: Path, monkeypatch
):
    """v3.9 (O3): cleanup unlink OSError preserves exception type/text."""
    from tools.base_tool import ToolResult

    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"fake-a")
    b.write_bytes(b"fake-b")
    out = tmp_path / "out.mp4"

    def fake_cut(self, inputs):
        op = str(inputs["output_path"])
        if "seg_0001" in op:
            return ToolResult(
                success=False,
                error="simulated primary trim failure",
                data={"operation": "cut"},
            )
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"good-temp")
        return ToolResult(success=True, data={"operation": "cut"})

    def boom_unlink(self, *args, **kwargs):
        raise OSError("boom-unlink-v3")

    monkeypatch.setattr(VideoTrimmer, "_cut", fake_cut)
    monkeypatch.setattr(Path, "unlink", boom_unlink)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {"input_path": str(a), "start_seconds": 0, "end_seconds": 1},
                {"input_path": str(b), "start_seconds": 0, "end_seconds": 1},
            ],
        }
    )
    assert not result.success
    assert "simulated primary trim failure" in (result.error or "")
    assert "boom-unlink-v3" not in (result.error or "")
    assert result.data["cleanup_ok"] is False
    assert "OSError" in (result.data["cleanup_error"] or "")
    assert "boom-unlink-v3" in (result.data["cleanup_error"] or "")
    details = result.data["temp_file_details"]
    assert any(d.get("error_type") == "OSError" for d in details)
    assert any("boom-unlink-v3" in (d.get("error_message") or "") for d in details)
    assert a.is_file() and b.is_file()


def test_concat_ownership_v3_rmdir_oserror_preserves_text(
    tmp_path: Path, monkeypatch
):
    """v3.10 (O3): cleanup rmdir OSError preserves exception type/text."""
    from tools.base_tool import ToolResult

    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"fake-a")
    b.write_bytes(b"fake-b")
    out = tmp_path / "out.mp4"

    def fake_cut(self, inputs):
        op = str(inputs["output_path"])
        if "seg_0001" in op:
            return ToolResult(
                success=False,
                error="simulated primary trim failure",
                data={"operation": "cut"},
            )
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"good-temp")
        return ToolResult(success=True, data={"operation": "cut"})

    def boom_rmdir(self):
        raise OSError("boom-rmdir-v3")

    monkeypatch.setattr(VideoTrimmer, "_cut", fake_cut)
    monkeypatch.setattr(Path, "rmdir", boom_rmdir)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {"input_path": str(a), "start_seconds": 0, "end_seconds": 1},
                {"input_path": str(b), "start_seconds": 0, "end_seconds": 1},
            ],
        }
    )
    assert not result.success
    assert "simulated primary trim failure" in (result.error or "")
    assert "boom-rmdir-v3" not in (result.error or "")
    assert result.data["temp_dir_exists_after"] is True
    assert result.data["cleanup_ok"] is False
    assert "OSError" in (result.data["cleanup_error"] or "")
    assert "boom-rmdir-v3" in (result.data["cleanup_error"] or "")
    assert a.is_file() and b.is_file()


def test_concat_ownership_v3_primary_preserved_despite_cleanup_diags(
    tmp_path: Path, monkeypatch
):
    """v3.11 (O3): primary trim error stays primary despite cleanup diags."""
    from tools.base_tool import ToolResult

    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"

    def fake_cut(self, inputs):
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"leftover-artifact")
        return ToolResult(
            success=False,
            error="simulated PRIMARY trim failure v3",
            data={"operation": "cut"},
        )

    def boom_unlink(self, *args, **kwargs):
        raise OSError("boom-unlink-primary-v3")

    def boom_rmdir(self):
        raise OSError("boom-rmdir-primary-v3")

    monkeypatch.setattr(VideoTrimmer, "_cut", fake_cut)
    monkeypatch.setattr(Path, "unlink", boom_unlink)
    monkeypatch.setattr(Path, "rmdir", boom_rmdir)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(a), "start_seconds": 0, "end_seconds": 1}],
        }
    )
    assert not result.success
    assert "simulated PRIMARY trim failure v3" in (result.error or "")
    assert "boom-unlink-primary-v3" not in (result.error or "")
    assert "boom-rmdir-primary-v3" not in (result.error or "")
    assert "boom-unlink-primary-v3" in (result.data["cleanup_error"] or "")
    assert "boom-rmdir-primary-v3" in (result.data["cleanup_error"] or "")
    assert a.is_file()


def test_concat_ownership_v3_partial_join_output_removed(tmp_path: Path, monkeypatch):
    """v3.12 (O4): invocation-created partial join output is removed."""
    from tools.base_tool import ToolResult

    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"

    def ok_cut(self, inputs):
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"good-temp")
        return ToolResult(success=True, data={"operation": "cut"})

    def join_writes_partial_then_raises(self, cmd: list[str], **kwargs):
        Path(cmd[-1]).write_bytes(b"PARTIAL-JOIN-OUTPUT")
        raise RuntimeError("simulated join failure after partial write")

    monkeypatch.setattr(VideoTrimmer, "_cut", ok_cut)
    monkeypatch.setattr(VideoTrimmer, "run_command", join_writes_partial_then_raises)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(a)}],
        }
    )
    assert not result.success
    assert "concat join failed" in (result.error or "")
    assert result.data["final_output_created"] is False
    assert result.data["output_existed_before"] is False
    assert result.data["output_created_by_invocation"] is True
    assert result.data["partial_output_removed"] is True
    assert result.data["partial_output_exists_after"] is False
    assert not out.exists()
    assert a.is_file()


def test_concat_ownership_v3_partial_removal_failure_reported(
    tmp_path: Path, monkeypatch
):
    """v3.13 (O4): unremovable partial join output is reported truthfully."""
    from tools.base_tool import ToolResult

    import tools.video.video_trimmer as vt_mod

    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"
    real_remover = vt_mod._try_remove_artifact

    def ok_cut(self, inputs):
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"good-temp")
        return ToolResult(success=True, data={"operation": "cut"})

    def join_writes_partial_then_raises(self, cmd: list[str], **kwargs):
        Path(cmd[-1]).write_bytes(b"PARTIAL-JOIN-OUTPUT")
        raise RuntimeError("simulated join failure")

    def selective_remover(p):
        if Path(p) == out:
            raise OSError("partial-blocked-v3")
        return real_remover(p)

    monkeypatch.setattr(VideoTrimmer, "_cut", ok_cut)
    monkeypatch.setattr(VideoTrimmer, "run_command", join_writes_partial_then_raises)
    monkeypatch.setattr(vt_mod, "_try_remove_artifact", selective_remover)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(a)}],
        }
    )
    assert not result.success
    assert "concat join failed" in (result.error or "")
    assert "partial-blocked-v3" not in (result.error or "")
    assert result.data["final_output_created"] is False
    assert result.data["output_created_by_invocation"] is True
    assert result.data["partial_output_removed"] is False
    assert result.data["partial_output_exists_after"] is True
    assert "partial-blocked-v3" in (result.data["cleanup_error"] or "")
    assert str(out) in result.data["cleanup_remaining_paths"]
    assert result.data["cleanup_ok"] is False
    assert out.is_file(), "unremovable partial must be reported, not hidden"
    assert a.is_file()


def test_concat_ownership_v3_preexisting_originals_never_owned(
    tmp_path: Path, monkeypatch
):
    """v3.14 (O1): pre-existing originals are never cleanup-owned."""
    from tools.base_tool import ToolResult
    from tools.base_tool import ToolCommandError

    tmpdir = tmp_path / ".concat_tmp"
    tmpdir.mkdir(parents=True)
    inner = tmpdir / "seg_0000.mp4"  # original living inside .concat_tmp
    inner.write_bytes(b"INNER-ORIGINAL")
    outer = tmp_path / "outer.mp4"
    outer.write_bytes(b"OUTER-ORIGINAL")
    out = tmp_path / "out.mp4"

    def boom_join(self, cmd: list[str], **kwargs):
        raise ToolCommandError(1, cmd, detail="simulated join failure")

    monkeypatch.setattr(VideoTrimmer, "run_command", boom_join)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(inner)}, {"input_path": str(outer)}],
        }
    )
    assert not result.success
    assert inner.is_file() and inner.read_bytes() == b"INNER-ORIGINAL"
    assert outer.is_file() and outer.read_bytes() == b"OUTER-ORIGINAL"
    owned = [d["path"] for d in result.data["temp_file_details"]]
    assert str(inner) not in owned
    assert str(outer) not in owned


@needs_ffmpeg
def test_concat_ownership_v3_normal_concat_unchanged(tmp_path: Path, dense_av: Path):
    """v3.15: normal successful concat unchanged (real FFmpeg)."""
    out = tmp_path / "v3_plain_join.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(dense_av)}, {"input_path": str(dense_av)}],
        }
    )
    assert result.success, result.error
    assert out.is_file()
    assert "video" in _streams_of(out)
    assert result.data["cleanup_attempted"] is True
    assert result.data["cleanup_ok"] is True
    assert not (out.parent / ".concat_tmp").exists()


@needs_ffmpeg
def test_concat_ownership_v3_aligned_trimmed_unchanged(
    tmp_path: Path, dense_av: Path
):
    """v3.16: aligned trimmed concat unchanged (real FFmpeg)."""
    out = tmp_path / "v3_aligned.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(dense_av),
                    "start_seconds": 0,
                    "end_seconds": 3,
                },
                {"input_path": str(dense_av)},
            ],
        }
    )
    assert result.success, result.error
    assert out.is_file()
    assert "video" in _streams_of(out)
    assert not (out.parent / ".concat_tmp").exists()


@needs_ffmpeg
def test_concat_ownership_v3_sparse_failure_unchanged(
    tmp_path: Path, sparse_av: Path, dense_av: Path
):
    """v3.17: sparse/non-keyframe failure unchanged (real FFmpeg)."""
    out = tmp_path / "v3_sparse.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {
                    "input_path": str(sparse_av),
                    "start_seconds": 1.7,
                    "end_seconds": 4.3,
                },
                {"input_path": str(dense_av)},
            ],
        }
    )
    assert not result.success
    err = (result.error or "").lower()
    assert "re-encode" in err and "libx264" in err
    assert result.data["failed_segment_index"] == 0
    assert result.data["final_output_created"] is False
    assert not out.exists()
    assert not (out.parent / ".concat_tmp").exists()
    assert sparse_av.is_file() and dense_av.is_file()


@needs_ffmpeg
def test_concat_ownership_v3_explicit_libx264_unchanged(
    tmp_path: Path, sparse_av: Path, dense_av: Path
):
    """v3.18: explicit codec='libx264' behavior unchanged (real FFmpeg)."""
    out = tmp_path / "v3_libx264.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "codec": "libx264",
            "segments": [
                {
                    "input_path": str(sparse_av),
                    "start_seconds": 1.7,
                    "end_seconds": 4.3,
                },
                {
                    "input_path": str(dense_av),
                    "start_seconds": 0,
                    "end_seconds": 2,
                },
            ],
        }
    )
    assert result.success, result.error
    assert out.is_file()
    assert "video" in _streams_of(out)
    assert not (out.parent / ".concat_tmp").exists()


@needs_ffmpeg
def test_concat_ownership_v3_cut_unchanged(tmp_path: Path, dense_av: Path, sparse_av: Path):
    """v3.19: _cut() behavior unchanged by the ownership repair."""
    ok_out = tmp_path / "v3_cut_ok.mp4"
    ok_result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(dense_av),
            "output_path": str(ok_out),
            "start_seconds": 0,
            "end_seconds": 3,
            "codec": "copy",
        }
    )
    assert ok_result.success, ok_result.error
    assert "video" in _streams_of(ok_out)
    assert ok_result.data["keyframe_aligned"] is True

    bad_out = tmp_path / "v3_cut_bad.mp4"
    bad_result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(sparse_av),
            "output_path": str(bad_out),
            "start_seconds": 1.7,
            "end_seconds": 4.3,
            "codec": "copy",
        }
    )
    assert not bad_result.success
    assert not bad_out.exists()
    assert bad_result.data["keyframe_aligned"] is False


@needs_ffmpeg
def test_concat_ownership_v3_speed_unchanged(tmp_path: Path, dense_av: Path):
    """v3.20: _speed() behavior unchanged by the ownership repair."""
    out = tmp_path / "v3_fast.mp4"
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


# ------------------------------------------------------------------
# Concat ownership corrective repair v4 (F1/F2/F3):
# regular-file segment gate, concat-local cleanup diagnostics without
# shared side-channel state, collision-safe concat-list reservation.
# ------------------------------------------------------------------


def test_concat_v4_directory_segment_rejected_before_ffmpeg(tmp_path: Path, monkeypatch):
    """v4.1 (F1): a directory segment fails before any FFmpeg execution."""
    from tools.base_tool import ToolResult

    calls: list[list[str]] = []
    cut_calls: list[dict] = []

    def spy_run(self, cmd: list[str], **kwargs):
        calls.append(list(cmd))
        return None

    def boom_cut(self, inputs: dict):
        cut_calls.append(dict(inputs))
        return ToolResult(success=True, data={"operation": "cut"})

    monkeypatch.setattr(VideoTrimmer, "run_command", spy_run)
    monkeypatch.setattr(VideoTrimmer, "_cut", boom_cut)
    subdir = tmp_path / "somedir"
    subdir.mkdir()
    out = tmp_path / "out.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(subdir), "start_seconds": 0, "end_seconds": 1}],
        }
    )
    assert not result.success
    assert cut_calls == [], "no trim may execute for a directory segment"
    assert calls == [], "no FFmpeg (trim or join) may execute for a directory segment"
    assert result.data["failed_segment_index"] == 0
    assert result.data["segment_input"] == str(subdir)
    assert "directory" in (result.data["segment_error"] or "").lower()
    assert result.data["final_output_created"] is False
    assert not out.exists()


def test_concat_v4_directory_failure_retains_index_and_context(tmp_path: Path, monkeypatch):
    """v4.2 (F1): middle directory segment reports its index/context; no join."""
    calls: list[list[str]] = []

    def spy_run(self, cmd: list[str], **kwargs):
        calls.append(list(cmd))
        return None

    monkeypatch.setattr(VideoTrimmer, "run_command", spy_run)
    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    subdir = tmp_path / "somedir"
    subdir.mkdir()
    out = tmp_path / "out.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(a)}, {"input_path": str(subdir)}],
        }
    )
    assert not result.success
    assert result.data["failed_segment_index"] == 1
    assert result.data["segment_input"] == str(subdir)
    assert "directory" in (result.data["segment_error"] or "").lower()
    assert result.data["segment_file_kind"] == "directory"
    assert result.data["final_output_created"] is False
    assert result.data["cleanup_attempted"] is True
    assert calls == [], "the final join must never run after a segment failure"
    assert not out.exists()
    assert a.is_file()


@needs_ffmpeg
def test_concat_v4_symlink_to_regular_file_remains_valid(tmp_path: Path, dense_av: Path):
    """v4.3 (F1): a symlink to a valid regular file stays supported."""
    link = tmp_path / "link.mp4"
    link.symlink_to(dense_av)
    out = tmp_path / "v4_symlink.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(link)}, {"input_path": str(dense_av)}],
        }
    )
    assert result.success, result.error
    assert out.is_file()
    assert "video" in _streams_of(out)
    assert link.is_symlink(), "the symlink itself must survive"
    assert not (out.parent / ".concat_tmp").exists()


def test_concat_v4_exists_stat_oserror_retains_type_and_message(tmp_path: Path, monkeypatch):
    """v4.4 (F2): exists()/stat OSError keeps exception type/message."""
    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"

    def boom_exists(self):
        raise OSError("injected-stat-failure-v4")

    monkeypatch.setattr(Path, "exists", boom_exists)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(a)}],
        }
    )
    assert not result.success
    assert result.data["failed_segment_index"] == 0
    assert result.data["segment_input"] == str(a)
    assert result.data["segment_stat_error_type"] == "OSError"
    assert "injected-stat-failure-v4" in (result.data["segment_stat_error_message"] or "")
    blob = (result.error or "") + str(result.data)
    assert "OSError" in blob
    assert "injected-stat-failure-v4" in blob
    assert result.data["final_output_created"] is False


def test_concat_v4_exists_uncertainty_sets_cleanup_failure_truthfully(
    tmp_path: Path, monkeypatch
):
    """v4.5 (F2): stat uncertainty never becomes a clean-success claim."""
    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"

    def boom_exists(self):
        raise OSError("injected-stat-uncertainty-v4")

    monkeypatch.setattr(Path, "exists", boom_exists)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(a)}],
        }
    )
    assert not result.success
    assert result.data["cleanup_attempted"] is True
    assert result.data["cleanup_ok"] is False
    assert result.data["cleanup_error"] is not None
    assert "injected-stat-uncertainty-v4" in (result.data["cleanup_error"] or "")


@needs_ffmpeg
def test_concat_v4_foreign_list_survives_byte_for_byte(tmp_path: Path, dense_av: Path):
    """v4.6 (F3): pre-existing .concat_tmp/concat_list.txt is never touched."""
    out = tmp_path / "v4_foreign.mp4"
    ctmp = out.parent / ".concat_tmp"
    ctmp.mkdir(parents=True, exist_ok=True)
    foreign = ctmp / "concat_list.txt"
    sentinel = b"FOREIGN-SENTINEL-v4-do-not-touch-0123456789"
    foreign.write_bytes(sentinel)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(dense_av)}, {"input_path": str(dense_av)}],
        }
    )
    assert result.success, result.error
    assert out.is_file()
    assert foreign.is_file(), "foreign list must survive the invocation"
    assert foreign.read_bytes() == sentinel, "foreign list must survive byte-for-byte"


@needs_ffmpeg
def test_concat_v4_occupied_list_causes_unique_reservation(tmp_path: Path, dense_av: Path):
    """v4.7 (F3): an occupied concat_list.txt forces a unique list path."""
    out = tmp_path / "v4_reserve.mp4"
    ctmp = out.parent / ".concat_tmp"
    ctmp.mkdir(parents=True, exist_ok=True)
    (ctmp / "concat_list.txt").write_bytes(b"FOREIGN-v4")
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(dense_av)}],
        }
    )
    assert result.success, result.error
    used = result.data["concat_list_path"]
    assert used != str(ctmp / "concat_list.txt")
    assert used.endswith("concat_list_c01.txt"), f"unexpected reservation: {used}"
    assert (ctmp / "concat_list.txt").read_bytes() == b"FOREIGN-v4"


@needs_ffmpeg
def test_concat_v4_multiple_occupied_list_names_advance_safely(
    tmp_path: Path, dense_av: Path
):
    """v4.8 (F3): several occupied list candidates advance to a free name."""
    out = tmp_path / "v4_multi.mp4"
    ctmp = out.parent / ".concat_tmp"
    ctmp.mkdir(parents=True, exist_ok=True)
    occupants = {
        "concat_list.txt": b"FOREIGN-0-v4",
        "concat_list_c01.txt": b"FOREIGN-1-v4",
        "concat_list_c02.txt": b"FOREIGN-2-v4",
    }
    for name, payload in occupants.items():
        (ctmp / name).write_bytes(payload)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(dense_av)}],
        }
    )
    assert result.success, result.error
    used = result.data["concat_list_path"]
    assert used.endswith("concat_list_c03.txt"), f"unexpected reservation: {used}"
    for name, payload in occupants.items():
        assert (ctmp / name).read_bytes() == payload, f"{name} mutated"


@needs_ffmpeg
def test_concat_v4_foreign_list_never_cleanup_owned(tmp_path: Path, dense_av: Path):
    """v4.9 (F3): the foreign list never enters the owned cleanup collection."""
    out = tmp_path / "v4_owned.mp4"
    ctmp = out.parent / ".concat_tmp"
    ctmp.mkdir(parents=True, exist_ok=True)
    foreign = ctmp / "concat_list.txt"
    foreign.write_bytes(b"FOREIGN-OWNERSHIP-v4")
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(dense_av)}],
        }
    )
    assert result.success, result.error
    owned = [d["path"] for d in result.data["temp_file_details"]]
    assert str(foreign) not in owned
    assert str(foreign) not in (result.data["cleanup_remaining_paths"] or [])
    assert foreign.read_bytes() == b"FOREIGN-OWNERSHIP-v4"


@needs_ffmpeg
def test_concat_v4_invocation_list_cleaned_normally(tmp_path: Path, dense_av: Path):
    """v4.10 (F3): the invocation-created list is cleaned on success."""
    out = tmp_path / "v4_clean.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(dense_av)}],
        }
    )
    assert result.success, result.error
    used = Path(result.data["concat_list_path"])
    assert used.name == "concat_list.txt"
    assert not used.exists(), "invocation-created list must be cleaned"
    assert not (out.parent / ".concat_tmp").exists()
    assert result.data["cleanup_ok"] is True


def test_concat_v4_list_write_failure_preserves_truth(tmp_path: Path, monkeypatch):
    """v4.11 (F3): list-write failure keeps ownership/cleanup truthful."""
    import builtins

    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"
    calls: list[list[str]] = []

    def spy_run(self, cmd: list[str], **kwargs):
        calls.append(list(cmd))
        return None

    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        if "concat_list" in str(file):
            raise OSError("injected-list-write-failure-v4")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(VideoTrimmer, "run_command", spy_run)
    monkeypatch.setattr(builtins, "open", guarded_open)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(a)}],
        }
    )
    assert not result.success
    assert "concat list write failed" in (result.error or "")
    assert "injected-list-write-failure-v4" in (result.error or "")
    assert result.data["join_exception_type"] == "OSError"
    assert result.data["final_output_created"] is False
    assert result.data["cleanup_attempted"] is True
    assert result.data["concat_list_path"] is not None
    assert calls == [], "join must not run when the list cannot be written"
    assert not out.exists()
    assert a.is_file()


def test_concat_v4_no_stale_side_channel_attribution(tmp_path: Path, monkeypatch):
    """v4.12 (F2): concat cleanup never attributes stale shared last_error."""
    from tools.base_tool import ToolResult

    import tools.video.video_trimmer as vt_mod

    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"fake-a")
    b.write_bytes(b"fake-b")
    out = tmp_path / "out.mp4"

    def fake_cut(self, inputs):
        op = str(inputs["output_path"])
        if "seg_0000" in op:
            p = Path(inputs["output_path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"good-temp")
            return ToolResult(success=True, data={"operation": "cut"})
        return ToolResult(
            success=False, error="simulated primary trim failure v4", data={}
        )

    # Case 1: a non-raising double; stale sentinel must not leak in.
    monkeypatch.setattr(VideoTrimmer, "_cut", fake_cut)
    monkeypatch.setattr(vt_mod, "_try_remove_artifact", lambda p: (False, True))
    vt_mod._try_remove_artifact.last_error = {
        "error_type": "OSError",
        "error_message": "STALE-SENTINEL-v4-must-never-surface",
    }
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {"input_path": str(a), "start_seconds": 0, "end_seconds": 1},
                {"input_path": str(b), "start_seconds": 0, "end_seconds": 1},
            ],
        }
    )
    assert not result.success
    blob = (result.error or "") + str(result.data)
    assert "STALE-SENTINEL-v4-must-never-surface" not in blob
    assert result.data["cleanup_ok"] is False

    # Case 2: production remover path with a live unlink failure; the
    # live diagnostic wins and the stale sentinel stays absent.
    monkeypatch.setattr(vt_mod, "_try_remove_artifact", vt_mod._UNPATCHED_TRY_REMOVE)

    def boom_unlink(self, *args, **kwargs):
        raise OSError("live-unlink-v4")

    monkeypatch.setattr(Path, "unlink", boom_unlink)
    vt_mod._try_remove_artifact.last_error = {
        "error_type": "OSError",
        "error_message": "STALE-SENTINEL-v4-must-never-surface",
    }
    out2 = tmp_path / "out2.mp4"
    result2 = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out2),
            "segments": [
                {"input_path": str(a), "start_seconds": 0, "end_seconds": 1},
                {"input_path": str(b), "start_seconds": 0, "end_seconds": 1},
            ],
        }
    )
    assert not result2.success
    blob2 = (result2.error or "") + str(result2.data)
    assert "STALE-SENTINEL-v4-must-never-surface" not in blob2
    assert "live-unlink-v4" in (result2.data["cleanup_error"] or "")


def test_concat_v4_temp_collision_advances_safely(tmp_path: Path, monkeypatch):
    """v4.13: on-disk temp-name collision reserves a distinct owned path."""
    from tools.base_tool import ToolResult

    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"
    ctmp = tmp_path / ".concat_tmp"
    ctmp.mkdir(parents=True)
    squatter = ctmp / "seg_0000.mp4"
    squatter.write_bytes(b"SQUATTER-BYTES-v4")
    cut_outputs: list[str] = []

    def ok_cut(self, inputs):
        cut_outputs.append(str(inputs["output_path"]))
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"good-temp")
        return ToolResult(success=True, data={"operation": "cut"})

    monkeypatch.setattr(VideoTrimmer, "_cut", ok_cut)
    monkeypatch.setattr(VideoTrimmer, "run_command", lambda self, cmd, **kw: None)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(a), "start_seconds": 0, "end_seconds": 1}],
        }
    )
    assert result.success, result.error
    assert len(cut_outputs) == 1
    assert cut_outputs[0] != str(squatter)
    assert "seg_0000" in cut_outputs[0]
    assert squatter.is_file()
    assert squatter.read_bytes() == b"SQUATTER-BYTES-v4"


def test_concat_v4_malformed_middle_cleanup_correct(tmp_path: Path, monkeypatch):
    """v4.14: malformed middle segment cleans owned temps, keeps context."""
    from tools.base_tool import ToolResult

    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"

    def fake_cut(self, inputs):
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"good-temp")
        return ToolResult(success=True, data={"operation": "cut"})

    monkeypatch.setattr(VideoTrimmer, "_cut", fake_cut)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {"input_path": str(a), "start_seconds": 0, "end_seconds": 1},
                {"not": "a-segment"},
            ],
        }
    )
    assert not result.success
    assert result.data["failed_segment_index"] == 1
    assert result.data["malformed_segment"] is True
    assert result.data["final_output_created"] is False
    assert list(tmp_path.rglob("seg_*.mp4")) == []
    assert not (tmp_path / ".concat_tmp").exists()
    assert a.is_file()


def test_concat_v4_preexisting_output_left_in_place(tmp_path: Path, monkeypatch):
    """v4.15 (O4): pre-existing final output survives a failed join."""
    from tools.base_tool import ToolResult
    from tools.base_tool import ToolCommandError

    a = tmp_path / "a.mp4"
    a.write_bytes(b"fake-a")
    out = tmp_path / "out.mp4"
    out.write_bytes(b"PREEXISTING-OUTPUT-v4")

    def fake_cut(self, inputs):
        p = Path(inputs["output_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"good-temp")
        return ToolResult(success=True, data={"operation": "cut"})

    def boom_join(self, cmd: list[str], **kwargs):
        raise ToolCommandError(1, cmd, detail="simulated join failure v4")

    monkeypatch.setattr(VideoTrimmer, "_cut", fake_cut)
    monkeypatch.setattr(VideoTrimmer, "run_command", boom_join)
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [{"input_path": str(a)}],
        }
    )
    assert not result.success
    assert "concat join failed" in (result.error or "")
    assert result.data["final_output_created"] is False
    assert result.data["output_existed_before"] is True
    assert result.data["output_created_by_invocation"] is False
    assert result.data["partial_output_removed"] is None
    assert out.is_file()
    assert a.is_file()


@needs_ffmpeg
def test_concat_v4_aligned_trimmed_concat_unchanged(tmp_path: Path, dense_av: Path):
    """v4.16: aligned trimmed concat succeeds with verified media."""
    out = tmp_path / "v4_aligned.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {"input_path": str(dense_av), "start_seconds": 0, "end_seconds": 3},
                {"input_path": str(dense_av)},
            ],
        }
    )
    assert result.success, result.error
    assert out.is_file()
    streams = _streams_of(out)
    assert "video" in streams and "audio" in streams
    assert result.data["cleanup_ok"] is True
    assert not (out.parent / ".concat_tmp").exists()


@needs_ffmpeg
def test_concat_v4_sparse_copy_rejection_unchanged(
    tmp_path: Path, sparse_av: Path, dense_av: Path
):
    """v4.17: sparse non-keyframe copy trim still fails closed, no join artifact."""
    out = tmp_path / "v4_sparse.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "segments": [
                {"input_path": str(sparse_av), "start_seconds": 1.7, "end_seconds": 4.3},
                {"input_path": str(dense_av)},
            ],
        }
    )
    assert not result.success
    err = (result.error or "").lower()
    assert "re-encode" in err and "libx264" in err
    assert result.data["failed_segment_index"] == 0
    assert result.data["final_output_created"] is False
    assert not out.exists()
    assert sparse_av.is_file() and dense_av.is_file()


@needs_ffmpeg
def test_concat_v4_explicit_libx264_unchanged(
    tmp_path: Path, sparse_av: Path, dense_av: Path
):
    """v4.18: explicit codec='libx264' still yields frame-accurate success."""
    out = tmp_path / "v4_libx264.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "concat",
            "output_path": str(out),
            "codec": "libx264",
            "segments": [
                {"input_path": str(sparse_av), "start_seconds": 1.7, "end_seconds": 4.3},
                {"input_path": str(dense_av), "start_seconds": 0, "end_seconds": 2},
            ],
        }
    )
    assert result.success, result.error
    assert out.is_file()
    assert "video" in _streams_of(out)
    assert not (out.parent / ".concat_tmp").exists()


def test_concat_v4_cut_interval_contract_unchanged(tmp_path: Path, dense_av: Path, monkeypatch):
    """v4.19: _cut() interval validation still rejects before FFmpeg."""
    def _must_not_run(self, cmd: list[str], **kwargs):
        raise AssertionError("FFmpeg must not run for an invalid interval")

    monkeypatch.setattr(VideoTrimmer, "run_command", _must_not_run)
    out = tmp_path / "v4_cut_bad.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "cut",
            "input_path": str(dense_av),
            "output_path": str(out),
            "start_seconds": 4.0,
            "end_seconds": 2.0,
            "codec": "copy",
        }
    )
    assert not result.success
    assert "end_seconds" in (result.error or "")
    assert not out.exists(), "invalid interval must not create an artifact"


def test_concat_v4_speed_contract_unchanged(tmp_path: Path, dense_av: Path, monkeypatch):
    """v4.20: _speed() contract unchanged (factor echo, output record)."""
    seen: list[list[str]] = []

    def fake_run(self, cmd: list[str], **kwargs):
        seen.append(list(cmd))
        return None

    monkeypatch.setattr(VideoTrimmer, "run_command", fake_run)
    out = tmp_path / "v4_speed.mp4"
    result = VideoTrimmer().execute(
        {
            "operation": "speed",
            "input_path": str(dense_av),
            "output_path": str(out),
            "speed_factor": 2.0,
        }
    )
    assert result.success, result.error
    assert result.data["speed_factor"] == 2.0
    assert result.data["output"] == str(out)
    assert len(seen) == 1
