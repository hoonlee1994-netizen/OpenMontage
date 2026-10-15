"""FFmpeg option scoping of the GPU translation (L5 full-scope finding F2).

FFmpeg resolves a per-stream option by walking the options in command-line
order and keeping the *last* one whose stream specifier matches the stream, so
``-c copy -c:v libx265`` re-encodes video while ``-c:v libx265 -c copy`` copies
it. The translator must reproduce that, never silently change the requested
codec / pixel format, and fall back to the original CPU command when it cannot
establish the equivalence. The oracle tests below run the *real* FFmpeg.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from lib.gpu import ffmpeg_exec
from lib.gpu.ffmpeg_args import _canon, parse, plan_output, translate

HW = {"h264", "hevc"}
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


FFMPEG = _real_binary("ffmpeg")
FFPROBE = _real_binary("ffprobe")


def _value_after(argv, flag):
    return argv[argv.index(flag) + 1]


# --------------------------------------------------------------------------
# Pure planner / translator behaviour
# --------------------------------------------------------------------------

def test_reported_command_keeps_copy_for_other_streams_and_encodes_video():
    """The reported F2 command: ``-c copy`` must not override the video encoder."""
    argv = ["-y", "-i", "src.mkv", "-c", "copy", "-c:v", "libx265", "-pix_fmt", "yuv420p", "out.mp4"]
    gpu, plans = translate(argv, available=HW)
    assert plans[0].action == "gpu" and plans[0].hw_codec == "hevc"
    # ``-c copy`` stays (audio keeps copying) and precedes the video encoder.
    assert gpu.index("-c") < gpu.index("-c:v")
    assert _value_after(gpu, "-c") == "copy"
    assert _value_after(gpu, "-c:v") == "hevc_mediacodec"


def test_video_codec_followed_by_copy_is_a_stream_copy():
    """``-c:v libx264 -c copy``: the later ``-c copy`` wins for video -> nothing to encode."""
    argv = ["-y", "-i", "src.mkv", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c", "copy", "out.mp4"]
    gpu, plans = translate(argv, available=HW)
    assert gpu is None and plans[0].action == "none" and "copy" in plans[0].reason


def test_video_codec_rewrite_keeps_key_and_position():
    gpu, plans = translate(["-y", "-i", "a.mkv", "-vcodec", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "o.mp4"],
                           available=HW)
    assert "-vcodec" in gpu and _value_after(gpu, "-vcodec") == "h264_mediacodec"
    assert "-c:v" not in gpu  # the original option was rewritten, not duplicated
    assert gpu.index("-vcodec") < gpu.index("-c:a")


def test_implicit_encoder_is_named_explicitly():
    gpu, _ = translate(["-y", "-i", "a.mkv", "-c:a", "aac", "-pix_fmt", "yuv420p", "o.mp4"], available=HW)
    assert _value_after(gpu, "-c:v") == "h264_mediacodec" and _value_after(gpu, "-c:a") == "aac"


def test_audio_codec_options_do_not_reach_video():
    argv = ["-y", "-i", "a.mkv", "-c:v", "libx264", "-c:a", "copy", "-pix_fmt", "yuv420p", "o.mp4"]
    gpu, plans = translate(argv, available=HW)
    assert plans[0].action == "gpu" and _value_after(gpu, "-c:a") == "copy"


@pytest.mark.parametrize("opts", [
    ["-c", "copy", "-c:0", "libx264"],                 # stream index
    ["-c", "copy", "-c:#0x100", "libx264"],            # stream id
    ["-c:v", "libx264", "-c:m:language:eng", "copy"],  # metadata specifier
    ["-c:v", "libx264", "-c:disp:default", "copy"],    # disposition specifier
    ["-c:V:1", "libx264"],                             # second video stream
])
def test_unresolvable_stream_specifiers_keep_the_original_cpu_command(opts):
    gpu, plans = translate(["-y", "-i", "a.mkv", *opts, "-pix_fmt", "yuv420p", "o.mp4"], available=HW)
    assert gpu is None and plans[0].action == "cpu"
    assert "cannot be resolved statically" in plans[0].reason


def test_first_video_stream_specifier_needs_a_single_video_stream():
    # No -map: one video stream, so -c:v:0 and -c:v are the same stream.
    gpu, plans = translate(["-y", "-i", "a.mkv", "-c:v:0", "libx264", "-pix_fmt", "yuv420p", "o.mp4"],
                           available=HW)
    assert plans[0].action == "gpu" and _value_after(gpu, "-c:v:0") == "h264_mediacodec"
    # With -map the number of video streams is not known: stay on the CPU.
    gpu, plans = translate(["-y", "-i", "a.mkv", "-map", "0", "-c:v:0", "libx264", "-pix_fmt", "yuv420p", "o.mp4"],
                           available=HW)
    assert gpu is None and plans[0].action == "cpu"


def test_pixel_format_scoped_to_another_stream_type_is_not_the_video_format():
    # -pix_fmt:a does not exist for video; the video has no explicit format -> CPU.
    gpu, plans = translate(["-y", "-i", "a.mkv", "-c:v", "libx264", "-pix_fmt:a", "yuv420p", "o.mp4"],
                           available=HW)
    assert gpu is None and "pixel format not specified" in plans[0].reason


def test_two_pass_encoding_stays_on_cpu():
    gpu, plans = translate(["-y", "-i", "a.mkv", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-b:v", "2M",
                            "-pass", "2", "o.mp4"], available=HW)
    assert gpu is None and "two-pass" in plans[0].reason


def test_expectation_is_attached_to_every_hardware_plan():
    _, plans = translate(["-y", "-i", "a.mkv", "-c:v", "libx265", "-pix_fmt", "yuv420p", "o.mp4"], available=HW)
    assert plans[0].expect["codec"] == "hevc" and plans[0].expect["encoder"] == "hevc_mediacodec"
    assert "yuv420p" in plans[0].expect["pix_fmts"]


def test_canonical_option_names():
    assert _canon("-c:v") == ("c", "v") and _canon("-vcodec") == ("c", "v")
    assert _canon("-codec:a:1") == ("c", "a:1") and _canon("-vb") == ("b", "v")
    assert _canon("-pix_fmt") == ("pix_fmt", "")


# --------------------------------------------------------------------------
# Output contract verification (before anything is published)
# --------------------------------------------------------------------------

def _stub_probe(monkeypatch, streams):
    from pathlib import Path
    from types import SimpleNamespace

    def run(cmd, **kwargs):
        sel = [s for s in streams if s.get("codec_type") == "video"] if "-select_streams" in cmd else streams
        if "v:0" in cmd:
            sel = sel[:1]
        return SimpleNamespace(stdout=json.dumps({"streams": sel}))

    ffmpeg_exec._load_light()
    monkeypatch.setattr(ffmpeg_exec, "Path", Path)
    monkeypatch.setattr(ffmpeg_exec, "json", json)
    monkeypatch.setattr(ffmpeg_exec, "_cpu_env", lambda: {})
    monkeypatch.setattr(ffmpeg_exec, "subprocess", SimpleNamespace(run=run))


EXPECT_HEVC = {"codec": "hevc", "encoder": "hevc_mediacodec", "pix_fmts": ("yuv420p", "yuvj420p")}


def test_validate_rejects_a_codec_that_is_not_the_requested_one(tmp_path, monkeypatch):
    f = tmp_path / "o.mp4"
    f.write_bytes(b"x")
    _stub_probe(monkeypatch, [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
                               "width": 64, "height": 64}])
    problem = ffmpeg_exec._validate([(str(f), "video", EXPECT_HEVC)])
    assert problem and "output contract mismatch" in problem and "h264/yuv420p" in problem


def test_validate_rejects_a_pixel_format_that_is_not_8bit_420(tmp_path, monkeypatch):
    f = tmp_path / "o.mp4"
    f.write_bytes(b"x")
    _stub_probe(monkeypatch, [{"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv444p10le",
                               "width": 64, "height": 64}])
    problem = ffmpeg_exec._validate([(str(f), "video", EXPECT_HEVC)])
    assert problem and "output contract mismatch" in problem and "yuv444p10le" in problem


def test_validate_accepts_the_requested_codec_and_records_what_it_saw(tmp_path, monkeypatch):
    f = tmp_path / "o.mp4"
    f.write_bytes(b"x")
    _stub_probe(monkeypatch, [{"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p",
                               "width": 64, "height": 64}])
    ffmpeg_exec._VERIFIED.clear()
    assert ffmpeg_exec._validate([(str(f), "video", EXPECT_HEVC)]) is None
    assert ffmpeg_exec._VERIFIED[str(f)] == "hevc/yuv420p"


def test_validate_ignores_cover_art_but_checks_every_real_video_stream(tmp_path, monkeypatch):
    f = tmp_path / "o.mp4"
    f.write_bytes(b"x")
    cover = {"codec_type": "video", "codec_name": "mjpeg", "pix_fmt": "yuvj420p", "width": 8, "height": 8,
             "disposition": {"attached_pic": 1}}
    good = {"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p", "width": 64, "height": 64}
    _stub_probe(monkeypatch, [good, cover])
    assert ffmpeg_exec._validate([(str(f), "video", EXPECT_HEVC)]) is None
    bad = dict(good, codec_name="h264")
    _stub_probe(monkeypatch, [good, bad])
    assert "mismatch" in ffmpeg_exec._validate([(str(f), "video", EXPECT_HEVC)])


def test_staged_media_attaches_the_expectation_by_final_path():
    pairs = [("/o/a.mp4", "/o/.t1.mp4"), ("/o/b.mp4", "/o/.t2.mp4")]
    media = {"/o/a.mp4": "video", "/o/b.mp4": "video"}
    out = ffmpeg_exec._staged_media(pairs, media, {"/o/a.mp4": EXPECT_HEVC})
    assert out[0] == ("/o/.t1.mp4", "video", EXPECT_HEVC) and out[1] == ("/o/.t2.mp4", "video")


# --------------------------------------------------------------------------
# Oracle: the planner and the translation agree with the real FFmpeg
# --------------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available")


def _has_encoders(*names):
    if not FFMPEG:
        return False
    out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return all(re.search(rf"\s{n}\s", out) for n in names)


needs_codecs = pytest.mark.skipif(
    not _has_encoders("libx264", "libx265", "mpeg4"), reason="libx264/libx265/mpeg4 not available")


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    """A tiny MPEG-4 video + AAC audio source: copy keeps ``mpeg4``, encodes show h264/hevc."""
    d = tmp_path_factory.mktemp("scoping")
    src = d / "src.mkv"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=0.4",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4", "-c:v", "mpeg4", "-c:a", "aac",
                    "-shortest", str(src)], check=True)
    return src


def _video_codec_of(src, opts, out):
    """The codec FFmpeg really gives the first output video stream, or None if it fails."""
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(src), *opts, str(out)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return None
    pr = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                         "stream=codec_name", "-of", "csv=p=0", str(out)], capture_output=True, text=True)
    return pr.stdout.strip() or None


ORDERINGS = [
    ["-c", "copy", "-c:v", "libx265"],
    ["-c:v", "libx265", "-c", "copy"],
    ["-c", "copy", "-c:v", "libx264"],
    ["-c:v", "libx264", "-c", "copy"],
    ["-c", "copy", "-vcodec", "libx264"],
    ["-vcodec", "libx264", "-c", "copy"],
    ["-vcodec", "libx264", "-c:v", "copy"],
    ["-c:v", "copy", "-vcodec", "libx265"],
    ["-codec", "copy", "-codec:v", "libx265"],
    ["-codec:v", "libx265", "-codec", "copy"],
    ["-c:v", "libx264", "-c:a", "copy"],
    ["-c:a", "copy", "-c:v", "libx265"],
    ["-c", "copy", "-c:v", "libx264", "-c:a", "aac"],
    ["-c:v", "libx264", "-c:v", "libx265"],
    ["-c:v", "libx265", "-c:v", "copy"],
    ["-c:v:0", "libx265"],
    ["-c", "copy", "-c:v:0", "libx264"],
    ["-c:v:0", "libx264", "-c", "copy"],
    ["-c:s", "mov_text", "-c:v", "libx264"],
    ["-c", "copy", "-c:s", "mov_text", "-c:v", "libx265"],
]


@needs_ffmpeg
@needs_codecs
@pytest.mark.parametrize("opts", ORDERINGS, ids=lambda o: " ".join(o))
def test_planner_and_translation_agree_with_real_ffmpeg(tmp_path, source, opts):
    pix = ["-pix_fmt", "yuv420p"]
    argv = ["-y", "-i", str(source), *opts, *pix, str(tmp_path / "orig.mp4")]
    truth = _video_codec_of(source, [*opts, *pix], tmp_path / "truth.mp4")
    assert truth in {"mpeg4", "h264", "hevc"}, (opts, truth)

    parsed = parse(argv)
    plan = plan_output(parsed.outputs[0], 0, parsed)
    if plan.action == "none":
        assert truth == "mpeg4", f"planner says no video encode but FFmpeg produced {truth}"
        assert translate(argv, available=HW)[0] is None
    elif plan.action == "gpu":
        assert truth == plan.hw_codec, f"planner expects {plan.hw_codec} but FFmpeg produced {truth}"
        gpu, _ = translate(argv, available=HW)
        # Stand the software encoders back in for the hardware ones (and drop the
        # hardware-only rate control): FFmpeg must then resolve the very same
        # video codec, i.e. the translation kept the options' order and scope.
        back = {"h264_mediacodec": "libx264", "hevc_mediacodec": "libx265"}
        run, skip = [], False
        for i, tok in enumerate(gpu[:-1]):
            if skip:
                skip = False
                continue
            if tok in ("-bitrate_mode", "-global_quality"):
                skip = True
                continue
            run.append(back.get(tok, tok))
        run[run.index("-i") + 1] = str(source)
        assert _video_codec_of(source, run[run.index(str(source)) + 1:], tmp_path / "round.mp4") == truth, \
            f"translation changed the effective codec: {' '.join(gpu)}"
    else:
        # Conservative CPU fallback is always allowed; the original command is untouched.
        assert translate(argv, available=HW)[0] is None
