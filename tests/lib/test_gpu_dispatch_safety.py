"""Data-safety rules of the GPU-first ffmpeg dispatcher's CPU fallback."""
from __future__ import annotations

import os

from lib.gpu import ffmpeg_exec


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
