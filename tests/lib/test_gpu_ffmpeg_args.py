"""Contract tests for the GPU-native FFmpeg translation (lib/gpu/ffmpeg_args).

The translator may only change the *video encoder* of an output and must
leave everything that defines the output contract untouched; anything it
cannot express faithfully on the hardware encoder stays on the CPU command.
"""
from __future__ import annotations

import pytest

from lib.gpu.ffmpeg_args import (crf_to_cq, own_outputs, parse, reads_stdin,
                                 renders_text, translate, writes_stdout)

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
    (["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "0", "o.mp4"], "lossless"),
    (["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-qp", "0", "o.mp4"], "lossless"),
    (["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
      "-x264-params", "lossless=1", "o.mp4"], "lossless"),
    (["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv444p", "o.mp4"], "pixel format"),
    (["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p10le", "o.mp4"], "pixel format"),
    (["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
      "-profile:v", "high444", "o.mp4"], "profile"),
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
    # A multi-file muxer writes several files from one output option: it is
    # never translated (and a mixed command that contains one is refused).
    ["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "hls", "out.m3u8"],
    ["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "segment", "out_%03d.ts"],
    ["-hide_banner", "-version"],
])
def test_commands_without_video_encode_are_left_alone(argv):
    gpu, plans = translate(argv, available=HW)
    assert gpu is None
    assert all(p.action == "none" for p in plans)


def test_implicit_default_encoder_for_mp4_is_translated():
    gpu, plans = translate(["-y", "-i", "in.mp4", "-vf", "scale=1280:-2", "-pix_fmt", "yuv420p", "out.mp4"],
                           available=HW)
    assert plans[0].source_encoder == "libx264"
    assert _value_after(gpu, "-c:v") == "h264_mediacodec"
    assert _value_after(gpu, "-global_quality") == str(crf_to_cq(23, "h264"))  # libx264 default CRF


def test_unspecified_pixel_format_stays_on_cpu():
    # Without -pix_fmt the CPU encoder derives the format from the input and
    # filter graph (10-bit stays 10-bit, 4:4:4 stays 4:4:4, some filters
    # promote 4:2:0 to 4:4:4). The hardware encoder only emits 8-bit 4:2:0,
    # so translating would silently change the output contract.
    gpu, plans = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-crf", "23", "out.mp4"], available=HW)
    assert gpu is None and plans[0].action == "cpu"
    assert "pixel format" in plans[0].reason


@pytest.mark.parametrize("container", ["mpegts", "ts", "mts", "flv", "3gp", "m4v"])
def test_implicit_non_h264_container_encoders_stay_on_cpu(container):
    # ffmpeg -h muxer=... : mpegts -> mpeg2video, flv -> flv1, 3gp -> h263,
    # m4v -> mpeg4. Translating an implicit default to H.264 changes the codec
    # family, so only the containers whose default really is H.264 move.
    gpu, plans = translate(["-y", "-i", "in.mp4", "-pix_fmt", "yuv420p", f"out.{container}"], available=HW)
    assert gpu is None and plans[0].action == "cpu"
    assert "implicit encoder" in plans[0].reason


def test_implicit_h264_containers_are_translated():
    for ext in ("mp4", "mov", "mkv"):
        gpu, plans = translate(["-y", "-i", "in.mp4", "-pix_fmt", "yuv420p", f"out.{ext}"], available=HW)
        assert plans[0].source_encoder == "libx264" and _value_after(gpu, "-c:v") == "h264_mediacodec"


def test_explicit_bitrate_uses_vbr_and_user_gop_is_kept():
    gpu, _ = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-b:v", "5M", "-g", "60", "o.mp4"],
                       available=HW, probe_fps=lambda path: 30.0)
    assert _value_after(gpu, "-bitrate_mode") == "vbr"
    assert "-global_quality" not in gpu
    assert gpu.count("-g") == 1 and _value_after(gpu, "-g") == "60"


def test_hevc_maps_to_hevc_hardware_encoder_and_keeps_tag():
    gpu, _ = translate(["-y", "-i", "in.mp4", "-c:v", "libx265", "-pix_fmt", "yuv420p",
                        "-crf", "28", "-tag:v", "hvc1", "o.mp4"], available=HW)
    assert _value_after(gpu, "-c:v") == "hevc_mediacodec"
    assert _value_after(gpu, "-tag:v") == "hvc1"


def test_unverified_hardware_encoder_keeps_cpu():
    gpu, plans = translate(["-y", "-i", "in.mp4", "-c:v", "libx265", "-pix_fmt", "yuv420p", "o.mp4"],
                           available={"h264"})
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
    argv = ["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "a.mp4",
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
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "12", "o.mp4"]
    gpu, plans = translate(argv, available=HW)
    assert gpu is None and "whole number of seconds" in plans[0].reason


def test_gop_whole_seconds_moves_to_hardware_and_is_kept():
    argv = ["-y", "-i", "in.mp4", "-vf", "fps=30", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-g", "60", "o.mp4"]
    gpu, plans = translate(argv, available=HW)
    assert plans[0].action == "gpu" and _value_after(gpu, "-g") == "60"


def test_gop_with_unknown_frame_rate_stays_on_cpu():
    gpu, plans = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                            "-g", "60", "o.mp4"], available=HW)
    assert gpu is None and "frame rate unknown" in plans[0].reason


def test_x264_keyint_is_carried_over_as_gop():
    argv = ["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-x264-params", "keyint=48:scenecut=0", "-r", "24", "o.mp4"]
    gpu, plans = translate(argv, available=HW)
    assert plans[0].action == "gpu" and _value_after(gpu, "-g") == "48"
    assert "-x264-params" not in gpu


def test_forced_keyframes_stay_on_cpu():
    gpu, plans = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                            "-force_key_frames", "expr:gte(t,n_forced*2)", "o.mp4"], available=HW)
    assert gpu is None and "forced keyframes" in plans[0].reason


def test_default_gop_is_bounded_by_x264_default():
    gpu, _ = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "o.mp4"],
                       available=HW)
    assert _value_after(gpu, "-g") == "250"


# --- colour range: reproduce the replaced CPU FFmpeg's semantics ----------

def test_legacy_ffmpeg_yuv420p_means_limited_range():
    gpu, _ = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "o.mp4"],
                       available=HW, reference="legacy")
    assert _value_after(gpu, "-color_range") == "tv"


def test_modern_ffmpeg_yuv420p_preserves_source_range():
    gpu, _ = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "o.mp4"],
                       available=HW, reference="modern")
    assert "-color_range" not in gpu


def test_yuvj420p_is_full_range_and_explicit_range_wins():
    gpu, _ = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuvj420p", "o.mp4"], available=HW)
    assert _value_after(gpu, "-pix_fmt") == "yuv420p" and _value_after(gpu, "-color_range") == "pc"
    gpu, _ = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-color_range", "pc", "o.mp4"], available=HW, reference="legacy")
    assert gpu.count("-color_range") == 1 and _value_after(gpu, "-color_range") == "pc"


def test_legacy_mp4_output_pins_constant_frame_rate():
    gpu, _ = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "o.mp4"],
                       available=HW, reference="legacy")
    assert _value_after(gpu, "-fps_mode") == "cfr"


def test_modern_reference_and_vfr_containers_keep_default_sync():
    gpu, _ = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "o.mp4"],
                       available=HW, reference="modern")
    assert "-fps_mode" not in gpu
    gpu, _ = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "o.mkv"],
                       available=HW, reference="legacy")
    assert "-fps_mode" not in gpu


def test_caller_sync_mode_is_respected():
    gpu, _ = translate(["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-vsync", "vfr", "o.mp4"], available=HW, reference="legacy")
    assert "-fps_mode" not in gpu and _value_after(gpu, "-vsync") == "vfr"


# --- text rendering must keep the Debian font stack (PRoot traced path) ----

@pytest.mark.parametrize("argv", [
    ["-y", "-i", "in.mp4", "-vf", "subtitles=a.srt", "-c:v", "libx264", "-pix_fmt", "yuv420p", "o.mp4"],
    ["-y", "-i", "in.mp4", "-filter_complex", "[0:v]ass=a.ass[v]", "-map", "[v]",
     "-c:v", "libx264", "-pix_fmt", "yuv420p", "o.mp4"],
    ["-y", "-i", "in.mp4", "-vf", "drawtext=text='hi':fontfile=/f.ttf", "-c:v", "libx264",
     "-pix_fmt", "yuv420p", "o.mp4"],
    ["-y", "-i", "in.mp4", "-vf", "drawtext=text='hi':fontsize=32", "-c:v", "libx264",
     "-pix_fmt", "yuv420p", "o.mp4"],
    ["-y", "-i", "in.mp4", "-vf", "subtitles=filename=subs.vtt:fontsdir=/fonts", "-c:v", "libx264",
     "-pix_fmt", "yuv420p", "o.mp4"],
])
def test_text_rendering_commands_are_detected(argv):
    assert renders_text(argv)


@pytest.mark.parametrize("argv", [
    ["-y", "-i", "in.mp4", "-vf", "scale=1280:-2,fps=30", "-c:v", "libx264", "-pix_fmt", "yuv420p", "o.mp4"],
    ["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "o.mp4"],
    ["-y", "-i", "subs.srt", "-i", "in.mp4", "-c:v", "copy", "-c:s", "mov_text", "o.mp4"],
    ["-y", "-i", "in.mp4", "-af", "loudnorm", "-c:v", "libx264", "-pix_fmt", "yuv420p", "o.mp4"],
    ["-y", "-i", "in.mp4", "-af", "bass=f=80,treble=f=80", "-c:v", "libx264",
     "-pix_fmt", "yuv420p", "o.mp4"],  # 'bass=' must not look like the ass filter
])
def test_non_text_commands_are_not_flagged(argv):
    assert not renders_text(argv)


# --- output ownership classification (the dispatcher's staging surface) -----

def test_own_outputs_marks_a_single_video_file_as_owned_video():
    o = own_outputs(["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "out.mp4"])[0]
    assert (o.kind, o.media, o.target) == ("file", "video", "out.mp4")


def test_own_outputs_marks_stream_copy_and_audio_outputs_as_media_files():
    outs = own_outputs(["-y", "-i", "in.mp4",
                        "-t", "1", "-c", "copy", "copy.mp4",
                        "-t", "1", "-vn", "-c:a", "aac", "audio.m4a"])
    assert [(o.kind, o.media) for o in outs] == [("file", "media"), ("file", "media")]


def test_own_outputs_keeps_a_single_image_file_but_flags_a_pattern():
    single = own_outputs(["-y", "-i", "in.mp4", "-frames:v", "1", "thumb.jpg"])[0]
    assert (single.kind, single.media) == ("file", "media")
    pattern = own_outputs(["-y", "-i", "in.mp4", "frame_%04d.png"])[0]
    assert pattern.kind == "unsafe"


@pytest.mark.parametrize("muxer,target", [
    ("hls", "out.m3u8"), ("segment", "out_%03d.ts"), ("dash", "out.mpd"), ("tee", "a.mp4"),
])
def test_own_outputs_flags_multi_file_muxers_as_unsafe(muxer, target):
    assert own_outputs(["-y", "-i", "in.mp4", "-f", muxer, target])[0].kind == "unsafe"


def test_own_outputs_leaves_pipes_discards_and_network_targets_alone():
    assert own_outputs(["-i", "in.mp4", "-f", "null", "-"])[0].kind == "pipe"
    assert own_outputs(["-i", "in.mp4", "pipe:1"])[0].kind == "pipe"
    assert own_outputs(["-y", "-i", "in.mp4", "-f", "null", "/dev/null"])[0].kind == "other"
    assert own_outputs(["-y", "-i", "in.mp4", "-f", "mp4", "rtmp://host/app"])[0].kind == "other"


def test_own_outputs_skips_a_non_regular_device_target():
    # /dev/null exists and is neither a file nor a directory: never staged over.
    assert own_outputs(["-y", "-i", "in.mp4", "-f", "mp4", "/dev/null"])[0].kind == "other"


def test_own_outputs_treats_a_checksum_muxer_as_a_plain_file():
    o = own_outputs(["-y", "-i", "in.mp4", "-f", "md5", "sums.md5"])[0]
    assert (o.kind, o.media) == ("file", "file")


def test_own_outputs_keeps_a_directory_target_ownable_so_publish_refuses_it(tmp_path):
    # A directory named as an output stays ownable: the transaction's pre-flight
    # refuses it, which is reported instead of letting ffmpeg error out.
    d = tmp_path / "blocked.mp4"
    d.mkdir()
    assert own_outputs(["-y", "-i", "in.mp4", str(d)])[0].kind == "file"
