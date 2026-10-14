"""Contract tests for the GPU-native FFmpeg translation (lib/gpu/ffmpeg_args).

The translator may only change the *video encoder* of an output and must
leave everything that defines the output contract untouched; anything it
cannot express faithfully on the hardware encoder stays on the CPU command.
"""
from __future__ import annotations

import pytest

from lib.gpu.ffmpeg_args import crf_to_cq, parse, reads_stdin, translate, writes_stdout

HW = {"h264", "hevc"}


def _value_after(argv, flag):
    return argv[argv.index(flag) + 1]


def test_segment_normalize_command_is_translated_with_everything_else_kept():
    argv = ["-y", "-ss", "1", "-t", "2", "-i", "in.mp4", "-filter:v", "scale=1920:1080,fps=30",
            "-c:v", "libx264", "-crf", "23", "-preset", "medium", "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "192k", "seg.mp4"]
    gpu, plans = translate(argv, available=HW)
    assert plans[0].action == "gpu"
    assert _value_after(gpu, "-c:v") == "h264_mediacodec"
    assert _value_after(gpu, "-bitrate_mode") == "cq"
    assert _value_after(gpu, "-global_quality") == str(crf_to_cq(23, "h264"))
    # x264-only knobs are dropped, the timeline/audio/filters are not.
    assert "-preset" not in gpu and "-crf" not in gpu
    for kept in (["-ss", "1"], ["-t", "2"], ["-filter:v", "scale=1920:1080,fps=30"], ["-r", "30"],
                 ["-c:a", "aac"], ["-b:a", "192k"], ["-pix_fmt", "yuv420p"]):
        i = gpu.index(kept[0])
        assert gpu[i:i + 2] == kept
    assert gpu[-1] == "seg.mp4"
    # Input section is byte-identical.
    assert gpu[:gpu.index("in.mp4") + 1] == argv[:argv.index("in.mp4") + 1]


@pytest.mark.parametrize("argv,reason", [
    (["-y", "-i", "in.mp4", "-c:v", "libx264", "-crf", "0", "o.mp4"], "lossless"),
    (["-y", "-i", "in.mp4", "-c:v", "libx264", "-qp", "0", "o.mp4"], "lossless"),
    (["-y", "-i", "in.mp4", "-c:v", "libx264", "-x264-params", "lossless=1", "o.mp4"], "lossless"),
    (["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv444p", "o.mp4"], "pixel format"),
    (["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p10le", "o.mp4"], "pixel format"),
    (["-y", "-i", "in.mp4", "-c:v", "libx264", "-profile:v", "high444", "o.mp4"], "profile"),
    (["-y", "-i", "in.mp4", "-c:v", "libvpx-vp9", "o.webm"], "no hardware encoder"),
    (["-y", "-i", "in.mp4", "-c:v", "prores_ks", "o.mov"], "no hardware encoder"),
])
def test_semantics_changing_requests_stay_on_cpu(argv, reason):
    gpu, plans = translate(argv, available=HW)
    assert gpu is None
    assert plans[0].action == "cpu"
    assert reason in plans[0].reason


@pytest.mark.parametrize("argv", [
    ["-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "out.mp4"],
    ["-y", "-i", "a.mp4", "-i", "b.wav", "-map", "0:v", "-map", "1:a", "-c:v", "copy", "o.mp4"],
    ["-i", "in.mp4", "-af", "silencedetect=n=-30dB:d=0.5", "-f", "null", "-"],
    ["-y", "-i", "in.mp4", "frames_%04d.png"],
    ["-y", "-i", "in.mp4", "-vn", "-c:a", "libmp3lame", "o.mp3"],
    ["-y", "-i", "in.mp4", "-ss", "3", "-frames:v", "1", "thumb.jpg"],
    ["-hide_banner", "-version"],
])
def test_commands_without_video_encode_are_left_alone(argv):
    gpu, plans = translate(argv, available=HW)
    assert gpu is None
    assert all(p.action == "none" for p in plans)


def test_implicit_default_encoder_for_mp4_is_translated():
    gpu, plans = translate(["-y", "-i", "in.mp4", "-vf", "scale=1280:-2", "out.mp4"], available=HW)
    assert plans[0].source_encoder == "libx264"
    assert _value_after(gpu, "-c:v") == "h264_mediacodec"
    assert _value_after(gpu, "-global_quality") == str(crf_to_cq(23, "h264"))  # libx264 default CRF


def test_explicit_bitrate_uses_vbr_and_user_gop_is_kept():
    gpu, _ = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-b:v", "5M", "-g", "60", "o.mp4"],
                       available=HW, probe_fps=lambda path: 30.0)
    assert _value_after(gpu, "-bitrate_mode") == "vbr"
    assert "-global_quality" not in gpu
    assert gpu.count("-g") == 1 and _value_after(gpu, "-g") == "60"


def test_hevc_maps_to_hevc_hardware_encoder_and_keeps_tag():
    gpu, _ = translate(["-y", "-i", "in.mp4", "-c:v", "libx265", "-crf", "28", "-tag:v", "hvc1", "o.mp4"],
                       available=HW)
    assert _value_after(gpu, "-c:v") == "hevc_mediacodec"
    assert _value_after(gpu, "-tag:v") == "hvc1"


def test_unverified_hardware_encoder_keeps_cpu():
    gpu, plans = translate(["-y", "-i", "in.mp4", "-c:v", "libx265", "o.mp4"], available={"h264"})
    assert gpu is None and plans[0].action == "cpu" and "not verified" in plans[0].reason


def test_remotion_prestitcher_stream_from_stdin():
    argv = ["-r", "30", "-f", "image2pipe", "-s", "1920x1080", "-vcodec", "mjpeg", "-i", "-",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium",
            "-video_track_timescale", "90000", "-crf", "18", "-y", "/tmp/pre.mp4"]
    gpu, plans = translate(argv, available=HW)
    assert reads_stdin(argv) and not writes_stdout(argv)
    # The -vcodec before -i is the *decoder* and must stay mjpeg.
    assert gpu[:gpu.index("-") + 1] == argv[:argv.index("-") + 1]
    assert _value_after(gpu, "-global_quality") == str(crf_to_cq(18, "h264"))
    assert _value_after(gpu, "-video_track_timescale") == "90000"


def test_multiple_outputs_are_planned_independently():
    argv = ["-y", "-i", "in.mp4", "-c:v", "libx264", "-crf", "20", "a.mp4",
            "-c:v", "libvpx-vp9", "b.webm", "-c", "copy", "c.mkv"]
    gpu, plans = translate(argv, available=HW)
    assert [p.action for p in plans] == ["gpu", "cpu", "none"]
    # b.webm and c.mkv segments are copied verbatim.
    assert gpu[gpu.index("a.mp4") + 1:] == argv[argv.index("a.mp4") + 1:]


def test_parser_handles_boolean_flags_and_negations():
    p = parse(["-y", "-noautorotate", "-re", "-i", "in.mp4", "-shortest", "-an", "o.mp4"])
    assert p.ok and [s.target for s in p.inputs] == ["in.mp4"] and [s.target for s in p.outputs] == ["o.mp4"]


def test_crf_mapping_is_monotonic_and_clamped():
    vals = [crf_to_cq(c, "h264") for c in range(0, 52)]
    assert all(a >= b for a, b in zip(vals, vals[1:]))
    assert crf_to_cq(0, "h264") == 100 and 0 < crf_to_cq(51, "h264") <= 100


# --- keyframe structure is part of the output contract -------------------

def test_gop_not_whole_seconds_stays_on_cpu():
    argv = ["-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=6",
            "-c:v", "libx264", "-g", "12", "o.mp4"]
    gpu, plans = translate(argv, available=HW)
    assert gpu is None and "whole number of seconds" in plans[0].reason


def test_gop_whole_seconds_moves_to_hardware_and_is_kept():
    argv = ["-y", "-i", "in.mp4", "-vf", "fps=30", "-c:v", "libx264", "-g", "60", "o.mp4"]
    gpu, plans = translate(argv, available=HW)
    assert plans[0].action == "gpu" and _value_after(gpu, "-g") == "60"


def test_gop_with_unknown_frame_rate_stays_on_cpu():
    gpu, plans = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-g", "60", "o.mp4"], available=HW)
    assert gpu is None and "frame rate unknown" in plans[0].reason


def test_x264_keyint_is_carried_over_as_gop():
    argv = ["-y", "-i", "in.mp4", "-c:v", "libx264", "-x264-params", "keyint=48:scenecut=0",
            "-r", "24", "o.mp4"]
    gpu, plans = translate(argv, available=HW)
    assert plans[0].action == "gpu" and _value_after(gpu, "-g") == "48"
    assert "-x264-params" not in gpu


def test_forced_keyframes_stay_on_cpu():
    gpu, plans = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-force_key_frames",
                            "expr:gte(t,n_forced*2)", "o.mp4"], available=HW)
    assert gpu is None and "forced keyframes" in plans[0].reason


def test_default_gop_is_bounded_by_x264_default():
    gpu, _ = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "o.mp4"], available=HW)
    assert _value_after(gpu, "-g") == "250"
