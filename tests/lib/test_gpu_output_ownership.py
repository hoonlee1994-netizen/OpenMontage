"""Output ownership of the FFmpeg dispatcher (L5 full-scope finding F1-a / F1-b).

A destination must be classified the way FFmpeg itself opens it: a ``file:`` URL
is the same file as the plain path, an extension-inferred muxer (``.m3u8``) is
the same multi-file muxer as ``-f hls``, and a destination that is also an
input or another output cannot be owned. Whatever the dispatcher cannot own, it
refuses before anything is written.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.gpu import ffmpeg_args, ffmpeg_exec
from lib.gpu.ffmpeg_args import local_path, own_outputs, plan_output, translate, url_scheme, write_target

HW = {"h264", "hevc"}


# --------------------------------------------------------------------------
# F1-a: local file URLs
# --------------------------------------------------------------------------

@pytest.mark.parametrize("token,path", [
    ("file:///tmp/a/existing.mp4", "/tmp/a/existing.mp4"),
    ("file:rel/out.mp4", "rel/out.mp4"),
    ("file:/abs/out.mp4", "/abs/out.mp4"),
    ("file:///tmp/a%20b.mp4", "/tmp/a%20b.mp4"),   # no percent-decoding, as in FFmpeg's file protocol
    ("file://localhost/tmp/x.mp4", "/localhost/tmp/x.mp4"),  # no special host part either
    ("plain/out.mp4", "plain/out.mp4"),
])
def test_local_path_strips_exactly_the_file_prefix(token, path):
    assert local_path(token) == path


@pytest.mark.parametrize("token", [
    "rtmp://host/app", "http://h/x.m3u8", "tee:a.mp4|b.mp4", "md5:sums.txt", "FILE:upper.mp4", "-", "pipe:1",
])
def test_non_file_tokens_have_no_local_path(token):
    assert local_path(token) is None


def test_url_scheme_follows_ffmpegs_protocol_detection():
    assert url_scheme("file:///x") == "file" and url_scheme("tee:a|b") == "tee"
    assert url_scheme("crypto+file:///x") == "crypto+file"
    assert url_scheme("out.mp4") is None and url_scheme("dir/file:x") is None


def test_file_url_output_is_an_owned_file_not_a_non_file_target(tmp_path):
    existing = tmp_path / "existing.mp4"
    existing.write_bytes(b"asset")
    o = own_outputs(["-y", "-i", "in.mp4", "-c", "copy", f"file://{existing}"])[0]
    assert o.kind == "file" and o.path == str(existing)


def test_file_url_and_plain_path_are_one_destination(tmp_path):
    out = tmp_path / "o.mp4"
    outs = own_outputs(["-y", "-i", "in.mp4", "-c", "copy", str(out), "-c", "copy", f"file://{out}"])
    assert [o.kind for o in outs] == ["unsafe", "unsafe"]
    assert "same destination" in outs[0].reason


def test_stage_cmd_redirects_a_file_url_and_publishes_to_the_plain_path(tmp_path):
    final = tmp_path / "existing.mp4"
    final.write_bytes(b"old")
    url = f"file://{final}"
    cmd = ["-y", "-i", "in.mp4", "-c", "copy", url]
    staged, pairs = ffmpeg_exec._stage_cmd(cmd, [url], has_y=True)
    (pair_final, temp), = pairs
    assert pair_final == str(final) and os.path.dirname(temp) == str(tmp_path)
    assert url not in staged and staged[-1] == temp
    Path(temp).write_bytes(b"new")
    assert ffmpeg_exec._commit_outputs(pairs) is None
    assert final.read_bytes() == b"new" and sorted(os.listdir(tmp_path)) == ["existing.mp4"]


def test_stage_cmd_resolves_a_relative_file_url_against_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel.mp4").write_bytes(b"old")
    staged, pairs = ffmpeg_exec._stage_cmd(["-y", "-i", "in.mp4", "file:rel.mp4"], ["file:rel.mp4"], has_y=True)
    assert pairs[0][0] == "rel.mp4"            # the file FFmpeg would have written
    assert not os.path.exists("file:rel.mp4")  # never a file literally named after the URL


def test_stage_cmd_follows_a_symlinked_output_to_the_file_it_writes(tmp_path):
    real = tmp_path / "real.mp4"
    real.write_bytes(b"asset")
    link = tmp_path / "link.mp4"
    link.symlink_to(real)
    staged, pairs = ffmpeg_exec._stage_cmd(["-y", "-i", "in.mp4", str(link)], [str(link)], has_y=True)
    assert pairs[0][0] == str(real)
    Path(pairs[0][1]).write_bytes(b"new")
    assert ffmpeg_exec._commit_outputs(pairs) is None
    assert link.is_symlink() and real.read_bytes() == b"new"   # the link survives, the asset is replaced
    assert write_target(str(link)) == str(real)


def test_stage_cmd_refuses_when_no_sibling_temp_can_be_created(tmp_path, monkeypatch):
    final = tmp_path / "locked.mp4"
    final.write_bytes(b"asset")
    monkeypatch.setattr(ffmpeg_exec.os, "access", lambda p, m: False)
    with pytest.raises(ffmpeg_exec._CannotStage) as exc:
        ffmpeg_exec._stage_cmd(["-y", "-i", "in.mp4", str(final)], [str(final)], has_y=True)
    assert exc.value.kind == "unstageable"


def test_a_missing_output_directory_is_left_to_the_encoder_to_report(tmp_path):
    final = tmp_path / "missing" / "o.mp4"
    staged, pairs = ffmpeg_exec._stage_cmd(["-y", "-i", "in.mp4", str(final)], [str(final)], has_y=True)
    assert pairs[0][0] == str(final)


# --------------------------------------------------------------------------
# F1-b: implicit multi-file muxers and sidecar files
# --------------------------------------------------------------------------

@pytest.mark.parametrize("explicit,implicit", [
    (["-f", "hls", "out.m3u8"], ["out.m3u8"]),
    (["-f", "dash", "out.mpd"], ["out.mpd"]),
    (["-f", "webm_chunk", "out.chk"], ["out.chk"]),
])
def test_implicit_multi_file_muxers_are_classified_like_the_explicit_form(explicit, implicit):
    base = ["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    a = own_outputs(base + explicit)[0]
    b = own_outputs(base + implicit)[0]
    assert a.kind == b.kind == "unsafe"
    assert plan_output(ffmpeg_args.parse(base + explicit).outputs[0], 0).action == "none"
    assert plan_output(ffmpeg_args.parse(base + implicit).outputs[0], 0).action == "none"


def test_implicit_hls_is_never_translated_or_staged_as_a_single_file():
    argv = ["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10",
            "-hls_time", "1", "-hls_segment_filename", "seg_%03d.ts", "playlist.m3u8"]
    gpu, plans = translate(argv, available=HW)
    assert gpu is None and plans[0].action == "none" and "hls" in plans[0].reason
    assert own_outputs(argv)[0].kind == "unsafe"


def test_a_file_url_playlist_is_still_the_implicit_hls_muxer():
    argv = ["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "file:///tmp/p.m3u8"]
    assert own_outputs(argv)[0].kind == "unsafe"


@pytest.mark.parametrize("opt", [
    ["-hls_segment_filename", "x_%d.ts"], ["-segment_list", "list.csv"], ["-init_seg_name", "i.m4s"],
    ["-media_seg_name", "m_$Number$.m4s"], ["-master_pl_name", "m.m3u8"], ["-hls_fmp4_init_filename", "i.mp4"],
])
def test_sidecar_writing_options_make_an_output_unownable(opt):
    argv = ["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", *opt, "out.mp4"]
    assert own_outputs(argv)[0].kind == "unsafe"
    assert translate(argv, available=HW)[0] is None


@pytest.mark.parametrize("target", ["tee:a.mp4|b.mp4", "md5:sums.txt", "crypto+file:///tmp/x.mp4"])
def test_protocols_that_write_local_files_are_unownable(target):
    assert own_outputs(["-y", "-i", "in.mp4", "-f", "mp4", target])[0].kind == "unsafe"


@pytest.mark.parametrize("target", ["rtmp://h/app", "srt://h:1", "udp://h:2", "http://h/x", "unix:///tmp/s"])
def test_network_targets_are_not_owned(target):
    assert own_outputs(["-y", "-i", "in.mp4", "-f", "mp4", target])[0].kind == "other"


def test_an_output_that_is_also_an_input_is_not_ownable(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    o = own_outputs(["-y", "-i", str(f), "-c", "copy", f"file://{f}"])[0]
    assert o.kind == "unsafe" and "also an input" in o.reason


# --------------------------------------------------------------------------
# The dispatcher refuses / owns accordingly (fake capability layer)
# --------------------------------------------------------------------------

class _Slot:
    def __enter__(self):
        return "slot"

    def __exit__(self, *_exc):
        return False


def _wire(monkeypatch, plans, gpu_argv, *, gpu_rc=1, cpu_rc=0, gpu_bytes=b"GPU-OUTPUT", cpu_bytes=b"CPU-OUTPUT",
          real_validate=False, mode="auto"):
    from lib.gpu import oop, runtime, telemetry

    calls = {"telemetry": []}

    def write_temps(cmd, payload):
        for arg in cmd:
            if isinstance(arg, str) and os.path.basename(arg).startswith(".om-gpu-"):
                Path(arg).write_bytes(payload)

    monkeypatch.setenv("OM_GPU_MODE", mode)
    monkeypatch.setattr(ffmpeg_exec, "real_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(ffmpeg_exec, "_cpu_env", lambda: dict(os.environ))
    monkeypatch.setattr(ffmpeg_exec, "_probe_fps", lambda path: None)
    monkeypatch.setattr(ffmpeg_exec, "_reference_semantics", lambda path: "modern")
    if not real_validate:
        monkeypatch.setattr(ffmpeg_exec, "_validate", lambda outputs: None)
    monkeypatch.setattr(ffmpeg_args, "translate", lambda a, **kw: (list(gpu_argv), list(plans)))
    monkeypatch.setattr(ffmpeg_args, "reads_stdin", lambda a: False)
    monkeypatch.setattr(ffmpeg_args, "writes_stdout", lambda a: False)
    monkeypatch.setattr(ffmpeg_args, "renders_text", lambda a: False)
    monkeypatch.setattr(runtime, "capabilities", lambda: {"ok": True, "hw_encoders": {"h264": {}, "hevc": {}}})
    monkeypatch.setattr(runtime, "hw_encoder", lambda codec: {"component": f"c2.mtk.{codec}.encoder", "max_instances": 1})
    monkeypatch.setattr(runtime, "healthy", lambda backend: True)
    monkeypatch.setattr(runtime, "active_encoder_jobs", lambda kind: 0)
    monkeypatch.setattr(runtime, "codec_slot", lambda *a, **k: _Slot())
    monkeypatch.setattr(runtime, "native_ffmpeg", lambda: "/data/native/ffmpeg")
    monkeypatch.setattr(runtime, "native_env", lambda: {})
    monkeypatch.setattr(runtime, "record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(oop, "warm", lambda: None)
    monkeypatch.setattr(telemetry, "record", lambda ev: calls["telemetry"].append(ev))

    def fake_native(cmd, **kw):
        calls["native_cmd"] = list(cmd)
        write_temps(cmd, gpu_bytes)
        return gpu_rc, b"gpu-log"

    def fake_owned(cmd, env, **kw):
        calls["cpu_cmd"] = list(cmd)
        write_temps(cmd, cpu_bytes)
        return cpu_rc, b"cpu-log"

    monkeypatch.setattr(ffmpeg_exec, "_run_native", fake_native)
    monkeypatch.setattr(ffmpeg_exec, "_run_owned", fake_owned)
    return calls


def _gpu_plan(target, codec="h264"):
    expect = {"codec": codec, "encoder": f"{codec}_mediacodec", "pix_fmts": ("yuv420p", "yuvj420p")}
    return SimpleNamespace(target=target, action="gpu", hw_codec=codec, reason="", expect=expect)


def test_dispatch_stages_a_file_url_output_and_a_failed_run_keeps_the_asset(tmp_path, monkeypatch):
    existing = tmp_path / "existing.mp4"
    existing.write_bytes(b"OLD-ASSET")
    video = tmp_path / "video.mp4"
    url = f"file://{existing}"
    argv = ["-y", "-i", "in.mp4", "-t", "1", "-c", "copy", url,
            "-t", "1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)]
    calls = _wire(monkeypatch, [_gpu_plan(str(video))], [a.replace("libx264", "h264_mediacodec") for a in argv],
                  gpu_rc=1, cpu_rc=1)
    assert ffmpeg_exec.dispatch(argv) != 0
    assert existing.read_bytes() == b"OLD-ASSET"                 # not truncated
    for cmd in (calls["native_cmd"], calls["cpu_cmd"]):
        assert url not in cmd and str(existing) not in cmd        # never handed to a writer directly
    assert os.listdir(tmp_path) == ["existing.mp4"]              # no temp left behind


def test_dispatch_publishes_a_file_url_output_over_the_plain_path(tmp_path, monkeypatch):
    existing = tmp_path / "existing.mp4"
    existing.write_bytes(b"OLD-ASSET")
    argv = ["-y", "-i", "in.mp4", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", f"file://{existing}"]
    _wire(monkeypatch, [_gpu_plan(f"file://{existing}")], [a.replace("libx264", "h264_mediacodec") for a in argv],
          gpu_rc=0)
    assert ffmpeg_exec.dispatch(argv) == 0
    assert existing.read_bytes() == b"GPU-OUTPUT"
    assert os.listdir(tmp_path) == ["existing.mp4"]


def test_dispatch_refuses_an_implicit_hls_output_before_running_anything(tmp_path, monkeypatch):
    """F1-b: the segment of an earlier run is a production asset the command must not touch."""
    segment = tmp_path / "production_000.ts"
    segment.write_bytes(b"VALID-SEGMENT")
    blocked = tmp_path / "blocked.mp4"
    blocked.mkdir()
    argv = ["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10",
            "-hls_time", "1", "-hls_segment_filename", str(tmp_path / "production_%03d.ts"),
            str(tmp_path / "playlist.m3u8"),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(blocked)]
    calls = _wire(monkeypatch, [_gpu_plan(str(blocked))], [a.replace("libx264", "h264_mediacodec") for a in argv])
    assert ffmpeg_exec.dispatch(argv) == 78
    assert "native_cmd" not in calls and "cpu_cmd" not in calls
    assert segment.read_bytes() == b"VALID-SEGMENT"
    assert sorted(os.listdir(tmp_path)) == ["blocked.mp4", "production_000.ts"]
    assert calls["telemetry"][-1]["backend"] == "gpu-failed"


def test_dispatch_refuses_a_destination_listed_twice(tmp_path, monkeypatch):
    out = tmp_path / "o.mp4"
    out.write_bytes(b"OLD")
    argv = ["-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", f"file://{out}"]
    calls = _wire(monkeypatch, [_gpu_plan(str(out)), _gpu_plan(f"file://{out}")], list(argv))
    assert ffmpeg_exec.dispatch(argv) == 78
    assert "native_cmd" not in calls and out.read_bytes() == b"OLD"


# --------------------------------------------------------------------------
# F2 at dispatch level: an output that is not what was asked for is not published
# --------------------------------------------------------------------------

def _probe_by_content(monkeypatch, table):
    """ffprobe stand-in: the probed file's bytes select the streams it 'contains'."""
    def run(cmd, **kwargs):
        data = Path(cmd[-1]).read_bytes()
        return SimpleNamespace(stdout=json.dumps({"streams": table.get(data, [])}))

    ffmpeg_exec._load_light()
    monkeypatch.setattr(ffmpeg_exec, "Path", Path)
    monkeypatch.setattr(ffmpeg_exec, "json", json)
    monkeypatch.setattr(ffmpeg_exec, "subprocess", SimpleNamespace(run=run))


def _video(codec, pix):
    return {"codec_type": "video", "codec_name": codec, "pix_fmt": pix, "width": 64, "height": 64}


def test_a_hardware_output_with_the_wrong_codec_is_not_published_and_runs_on_cpu(tmp_path, monkeypatch):
    out = tmp_path / "o.mp4"
    out.write_bytes(b"OLD-ASSET")
    argv = ["-y", "-i", "in.mkv", "-c", "copy", "-c:v", "libx265", "-pix_fmt", "yuv420p", str(out)]
    calls = _wire(monkeypatch, [_gpu_plan(str(out), "hevc")], list(argv), gpu_rc=0, real_validate=True)
    _probe_by_content(monkeypatch, {b"GPU-OUTPUT": [_video("h264", "yuv444p10le")],
                                    b"CPU-OUTPUT": [_video("hevc", "yuv420p")]})
    assert ffmpeg_exec.dispatch(argv) == 0
    assert out.read_bytes() == b"CPU-OUTPUT"                       # the CPU command produced what was asked
    last = calls["telemetry"][-1]
    assert last["backend"] == "cpu" and "output contract mismatch" in last["fallback_reason"]
    assert os.listdir(tmp_path) == ["o.mp4"]


def test_a_wrong_hardware_output_is_a_failure_in_require_mode(tmp_path, monkeypatch):
    out = tmp_path / "o.mp4"
    out.write_bytes(b"OLD-ASSET")
    argv = ["-y", "-i", "in.mkv", "-c:v", "libx265", "-pix_fmt", "yuv420p", str(out)]
    calls = _wire(monkeypatch, [_gpu_plan(str(out), "hevc")], list(argv), gpu_rc=0, real_validate=True,
                  mode="require")
    _probe_by_content(monkeypatch, {b"GPU-OUTPUT": [_video("h264", "yuv420p")]})
    assert ffmpeg_exec.dispatch(argv) != 0
    assert out.read_bytes() == b"OLD-ASSET" and "cpu_cmd" not in calls
    assert calls["telemetry"][-1]["backend"] == "gpu-failed"


def test_telemetry_names_the_codec_the_published_file_really_has(tmp_path, monkeypatch):
    out = tmp_path / "o.mp4"
    argv = ["-y", "-i", "in.mkv", "-c:v", "libx265", "-pix_fmt", "yuv420p", str(out)]
    calls = _wire(monkeypatch, [_gpu_plan(str(out), "hevc")], list(argv), gpu_rc=0, real_validate=True)
    _probe_by_content(monkeypatch, {b"GPU-OUTPUT": [_video("hevc", "yuv420p")]})
    assert ffmpeg_exec.dispatch(argv) == 0
    ev = calls["telemetry"][-1]
    assert ev["backend"] == "gpu" and ev["encoder"] == "hevc_mediacodec"
    assert ev["verified"] == {str(out): "hevc/yuv420p"}


def test_telemetry_lists_software_encoded_outputs_of_a_mixed_command(tmp_path, monkeypatch):
    hw, sw = tmp_path / "hw.mp4", tmp_path / "sw.mp4"
    argv = ["-y", "-i", "in.mkv", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(hw),
            "-c:v", "libx264", "-pix_fmt", "yuv444p", str(sw)]
    plans = [_gpu_plan(str(hw)), SimpleNamespace(target=str(sw), action="cpu", reason="pixel format yuv444p")]
    calls = _wire(monkeypatch, plans, list(argv), gpu_rc=0)
    assert ffmpeg_exec.dispatch(argv) == 0
    ev = calls["telemetry"][-1]
    assert ev["backend"] == "gpu" and ev["software_outputs"] == [str(sw)]


def test_every_hardware_output_of_a_command_is_verified_against_its_own_codec(tmp_path, monkeypatch):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    argv = ["-y", "-i", "in.mkv", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(a),
            "-c:v", "libx265", "-pix_fmt", "yuv420p", str(b)]
    calls = _wire(monkeypatch, [_gpu_plan(str(a), "h264"), _gpu_plan(str(b), "hevc")], list(argv), gpu_rc=0,
                  real_validate=True)

    seen = []

    def run(cmd, **kwargs):                      # every output came out as H.264: wrong for the HEVC one
        seen.append(Path(cmd[-1]).name)
        return SimpleNamespace(stdout=json.dumps({"streams": [_video("h264", "yuv420p")]}))

    ffmpeg_exec._load_light()
    monkeypatch.setattr(ffmpeg_exec, "Path", Path)
    monkeypatch.setattr(ffmpeg_exec, "json", json)
    monkeypatch.setattr(ffmpeg_exec, "subprocess", SimpleNamespace(run=run))
    assert ffmpeg_exec.dispatch(argv) == 0               # the CPU fallback produced both
    last = calls["telemetry"][-1]
    assert last["backend"] == "cpu" and "output contract mismatch" in last["fallback_reason"]
    assert str(b) in last["fallback_reason"] and ".om-gpu-" not in last["fallback_reason"]   # names the final
    assert str(a) not in last["fallback_reason"]            # the H.264 output was fine


def test_telemetry_lists_every_encoder_and_device_of_a_multi_codec_command(tmp_path, monkeypatch):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    argv = ["-y", "-i", "in.mkv", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(a),
            "-c:v", "libx265", "-pix_fmt", "yuv420p", str(b)]
    calls = _wire(monkeypatch, [_gpu_plan(str(a), "h264"), _gpu_plan(str(b), "hevc")], list(argv), gpu_rc=0)
    assert ffmpeg_exec.dispatch(argv) == 0
    ev = calls["telemetry"][-1]
    assert ev["encoder"] == "h264_mediacodec,hevc_mediacodec"
    assert ev["device"] == "c2.mtk.h264.encoder,c2.mtk.hevc.encoder"
