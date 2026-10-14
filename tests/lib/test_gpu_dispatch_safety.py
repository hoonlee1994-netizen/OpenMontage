"""Data-safety rules of the GPU-first ffmpeg dispatcher's CPU fallback."""
from __future__ import annotations

import os
from types import SimpleNamespace

from lib.gpu import ffmpeg_exec


def _plan(target):
    return SimpleNamespace(target=target)


def test_stage_outputs_rewrites_to_a_sibling_temp_with_same_extension(tmp_path):
    final = str(tmp_path / "out.mp4")
    cmd = ["-y", "-i", "in.mp4", "-c:v", "h264_mediacodec", final]
    staged = ffmpeg_exec._stage_outputs(cmd, [_plan(final)])
    assert staged is not None
    staged_cmd, pairs = staged
    assert len(pairs) == 1 and pairs[0][0] == final
    temp = pairs[0][1]
    assert temp != final
    assert os.path.dirname(temp) == str(tmp_path)
    assert temp.endswith(".mp4")  # same muxer detection
    assert staged_cmd[-1] == temp and final not in staged_cmd


def test_stage_outputs_refuses_existing_file_without_overwrite_flag(tmp_path):
    final = tmp_path / "out.mp4"
    final.write_bytes(b"precious")
    # No -y: ffmpeg would refuse to overwrite; staging must not fake success.
    assert ffmpeg_exec._stage_outputs(["-i", "in.mp4", str(final)], [_plan(str(final))]) is None


def test_stage_outputs_allows_existing_file_with_yes(tmp_path):
    final = tmp_path / "out.mp4"
    final.write_bytes(b"old")
    staged = ffmpeg_exec._stage_outputs(["-y", "-i", "in.mp4", str(final)], [_plan(str(final))])
    assert staged is not None and staged[1][0][0] == str(final)


def test_stage_outputs_skips_pipe_targets(tmp_path):
    assert ffmpeg_exec._stage_outputs(["-i", "in.mp4", "pipe:1"], [_plan("pipe:1")]) is None


def test_stage_outputs_returns_none_when_target_token_is_absent(tmp_path):
    final = str(tmp_path / "out.mp4")
    assert ffmpeg_exec._stage_outputs(["-y", "-i", "in.mp4", "other.mp4"], [_plan(final)]) is None


def test_commit_publishes_atomically_and_discard_leaves_no_trace(tmp_path):
    final = tmp_path / "out.mp4"
    final.write_bytes(b"old")
    staged = ffmpeg_exec._stage_outputs(["-y", "-i", "in.mp4", str(final)], [_plan(str(final))])
    _cmd, pairs = staged
    temp = pairs[0][1]
    with open(temp, "wb") as fh:
        fh.write(b"new-and-valid")
    assert ffmpeg_exec._commit_outputs(pairs) is None
    assert final.read_bytes() == b"new-and-valid" and not os.path.exists(temp)


def test_discarded_attempt_never_touches_the_final_output(tmp_path):
    final = tmp_path / "user.mp4"
    final.write_bytes(b"asset")
    staged = ffmpeg_exec._stage_outputs(["-y", "-i", "in.mp4", str(final)], [_plan(str(final))])
    _cmd, pairs = staged
    with open(pairs[0][1], "wb") as fh:
        fh.write(b"partial garbage")
    ffmpeg_exec._discard_outputs(pairs)
    assert final.read_bytes() == b"asset"          # untouched user asset
    assert not os.path.exists(pairs[0][1])         # no leftover temp


def test_each_attempt_uses_a_distinct_temp(tmp_path):
    final = str(tmp_path / "out.mp4")
    cmd = ["-y", "-i", "in.mp4", final]
    first = ffmpeg_exec._stage_outputs(cmd, [_plan(final)])[1][0][1]
    second = ffmpeg_exec._stage_outputs(cmd, [_plan(final)])[1][0][1]
    assert first != second  # a retry can never race the previous writer


def test_fallback_never_deletes_untouched_preexisting_output(tmp_path):
    asset = tmp_path / "user_asset.mp4"
    asset.write_bytes(b"precious")
    before = ffmpeg_exec._snapshot([str(asset)])
    # GPU attempt failed without touching the file (e.g. refused to overwrite).
    ffmpeg_exec._remove_partial([str(asset)], before)
    assert asset.read_bytes() == b"precious"


def test_fallback_removes_file_the_gpu_attempt_created(tmp_path):
    out = tmp_path / "new.mp4"
    before = ffmpeg_exec._snapshot([str(out)])
    out.write_bytes(b"partial")
    ffmpeg_exec._remove_partial([str(out)], before)
    assert not out.exists()


def test_fallback_removes_file_the_gpu_attempt_rewrote(tmp_path):
    out = tmp_path / "rewritten.mp4"
    out.write_bytes(b"old")
    before = ffmpeg_exec._snapshot([str(out)])
    os.utime(out, ns=(1, 1))  # different mtime -> rewritten by the attempt
    out.write_bytes(b"partial-gpu-output")
    ffmpeg_exec._remove_partial([str(out)], before)
    assert not out.exists()


def test_real_ffmpeg_skips_the_shim_directory(monkeypatch, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    exe = fake / "ffmpeg"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.delenv("OM_GPU_REAL_FFMPEG", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([ffmpeg_exec._SHIM_DIR, str(fake)]))
    assert ffmpeg_exec.real_ffmpeg() == str(exe)
