"""A command with nothing to accelerate must still not destroy a production file.

L5 full-scope finding F1-c: Remotion's final stream-copy step (and any other
CPU-only FFmpeg command) used to be exec'd straight onto the destination, so a
failure or cancellation left a truncated file where the approved video was. These
tests drive the real dispatcher script with the real FFmpeg: a command that would
overwrite an existing regular file runs as an owned child writing a staged temp,
and only a successful run replaces the file.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

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


SHIM = REPO_ROOT / "gpu_native" / "shims" / "ffmpeg_dispatch.py"
FFMPEG = _real_binary("ffmpeg")
FFPROBE = _real_binary("ffprobe")

pytestmark = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available")


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def work(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def env(tmp_path):
    """An 'installed' runtime that is never consulted: these commands have nothing to accelerate."""
    home = tmp_path / "gpu-home"
    (home / "prefix" / "bin").mkdir(parents=True)
    fake = home / "prefix" / "bin" / "ffmpeg"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    e = dict(os.environ, OM_GPU_HOME=str(home), OM_GPU_MODE="auto", OM_GPU_OOP="0",
             OM_GPU_REAL_FFMPEG=FFMPEG, OM_GPU_EXEC_LOG=str(tmp_path / "exec.jsonl"))
    e.pop("OM_GPU_REAL_FFMPEG_ARGS", None)
    return e


@pytest.fixture
def clip(tmp_path):
    """A tiny valid mp4 used both as input and as the 'approved asset'."""
    out = tmp_path / "clip.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(out)], check=True)
    return out


def shim(args, env, cwd, **kw):
    return subprocess.run([sys.executable, "-I", "-S", str(SHIM), *args], env=env, cwd=cwd,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120, **kw)


def asset(work, clip, name="approved.mp4"):
    path = work / name
    shutil.copyfile(clip, path)
    return path


def leftovers(work, keep):
    return sorted(p.name for p in work.iterdir() if p.name not in keep)


def probe_streams(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return r.stdout.split()


def test_overwrite_by_a_stream_copy_publishes_atomically_and_leaves_no_temp(work, clip, env):
    out = asset(work, clip)
    before = sha(out)
    r = shim(["-y", "-v", "error", "-i", str(clip), "-t", "1", "-c", "copy", str(out)], env, work)
    assert r.returncode == 0, r.stderr
    assert sha(out) != before and probe_streams(out) == ["video", "audio"]
    assert leftovers(work, {out.name}) == []


def test_a_failure_after_the_output_was_opened_keeps_the_approved_asset(work, clip, env):
    """FFmpeg opens (and truncates) the first output before it fails on the second."""
    out = asset(work, clip)
    before = sha(out)
    r = shim(["-y", "-v", "error", "-i", str(clip), "-c", "copy", str(out),
              "-c", "copy", str(work / "no-such-dir" / "second.mp4")], env, work)
    assert r.returncode != 0
    assert sha(out) == before                      # without ownership this is a zero-byte file
    assert leftovers(work, {out.name}) == []


def test_the_exit_status_of_the_command_is_passed_through(work, clip, env):
    out = asset(work, clip)
    direct = subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(clip), "-c", "copy", str(out),
                             "-c", "copy", str(work / "no-such-dir" / "s.mp4")], capture_output=True)
    owned = shim(["-y", "-v", "error", "-i", str(clip), "-c", "copy", str(out),
                  "-c", "copy", str(work / "no-such-dir" / "s.mp4")], env, work)
    assert owned.returncode == direct.returncode != 0


def test_cancellation_mid_write_keeps_the_approved_asset(work, clip, env):
    out = asset(work, clip)
    before = sha(out)
    p = subprocess.Popen([sys.executable, "-I", "-S", str(SHIM), "-y", "-v", "error", "-re", "-i", str(clip),
                          "-c", "copy", str(out)], env=env, cwd=work, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not any(f.name.startswith(".om-gpu-") for f in work.iterdir()):
        time.sleep(0.01)
    assert any(f.name.startswith(".om-gpu-") for f in work.iterdir()), "the writer never started"
    time.sleep(0.3)                                # let it be genuinely mid-write
    p.send_signal(signal.SIGTERM)
    p.communicate(timeout=60)
    assert p.returncode == -signal.SIGTERM         # it terminates as the signal said, nothing published
    assert sha(out) == before
    assert leftovers(work, {out.name}) == []       # the staged temp was discarded


def test_killing_the_dispatcher_keeps_the_approved_asset_and_stops_the_writer(work, clip, env):
    out = asset(work, clip)
    before = sha(out)
    p = subprocess.Popen([sys.executable, "-I", "-S", str(SHIM), "-y", "-v", "error", "-re", "-i", str(clip),
                          "-c", "copy", str(out)], env=env, cwd=work, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not any(f.name.startswith(".om-gpu-") for f in work.iterdir()):
        time.sleep(0.01)
    time.sleep(0.3)
    p.kill()
    p.wait()
    assert sha(out) == before
    # The orphaned encoder dies with its dispatcher (PR_SET_PDEATHSIG): the staged
    # temp stops growing, so it can never be published by anything.
    temps = [f for f in work.iterdir() if f.name.startswith(".om-gpu-")]
    sizes = [f.stat().st_size for f in temps]
    time.sleep(1.0)
    assert [f.stat().st_size for f in temps] == sizes
    assert sha(out) == before


def test_a_retry_after_a_failed_run_succeeds_on_the_same_path(work, clip, env):
    out = asset(work, clip)
    before = sha(out)
    bad = shim(["-y", "-v", "error", "-i", str(clip), "-c", "copy", str(out),
                "-c", "copy", str(work / "no-such-dir" / "s.mp4")], env, work)
    assert bad.returncode != 0 and sha(out) == before
    good = shim(["-y", "-v", "error", "-i", str(clip), "-t", "1", "-c", "copy", str(out)], env, work)
    assert good.returncode == 0 and sha(out) != before and probe_streams(out) == ["video", "audio"]
    assert leftovers(work, {out.name}) == []


def test_every_output_of_a_multi_output_command_is_published_together(work, clip, env):
    a, b = asset(work, clip, "a.mp4"), work / "b.mp4"
    r = shim(["-y", "-v", "error", "-i", str(clip), "-t", "1", "-c", "copy", str(a),
              "-t", "1", "-c", "copy", str(b)], env, work)
    assert r.returncode == 0 and b.is_file() and probe_streams(a) == probe_streams(b) == ["video", "audio"]
    assert leftovers(work, {"a.mp4", "b.mp4"}) == []


def test_a_failed_multi_output_command_writes_none_of_its_outputs(work, clip, env):
    a, b = asset(work, clip, "a.mp4"), work / "b.mp4"
    before = sha(a)
    r = shim(["-y", "-v", "error", "-i", str(clip), "-c", "copy", str(a), "-c", "copy", str(b),
              "-c", "copy", str(work / "no-such-dir" / "c.mp4")], env, work)
    assert r.returncode != 0 and sha(a) == before and not b.exists()
    assert leftovers(work, {"a.mp4"}) == []


def test_a_file_url_destination_is_owned_like_a_plain_path(work, clip, env):
    out = asset(work, clip)
    before = sha(out)
    bad = shim(["-y", "-v", "error", "-i", str(clip), "-c", "copy", f"file://{out}",
                "-c", "copy", str(work / "no-such-dir" / "s.mp4")], env, work)
    assert bad.returncode != 0 and sha(out) == before
    ok = shim(["-y", "-v", "error", "-i", str(clip), "-t", "1", "-c", "copy", f"file://{out}"], env, work)
    assert ok.returncode == 0 and sha(out) != before and leftovers(work, {out.name}) == []


def test_a_relative_file_url_writes_the_relative_file(work, clip, env):
    out = asset(work, clip, "rel.mp4")
    r = shim(["-y", "-v", "error", "-i", str(clip), "-t", "1", "-c", "copy", "file:rel.mp4"], env, work)
    assert r.returncode == 0 and probe_streams(out) == ["video", "audio"]
    assert not (work / "file:rel.mp4").exists()


def test_a_symlinked_destination_keeps_its_link_and_replaces_the_target(work, clip, env):
    real = asset(work, clip, "real.mp4")
    link = work / "link.mp4"
    link.symlink_to(real)
    before = sha(real)
    r = shim(["-y", "-v", "error", "-i", str(clip), "-t", "1", "-c", "copy", str(link)], env, work)
    assert r.returncode == 0 and link.is_symlink() and sha(real) != before
    assert leftovers(work, {"real.mp4", "link.mp4"}) == []


def test_without_dash_y_ffmpegs_own_refusal_is_preserved(work, clip, env):
    out = asset(work, clip)
    before = sha(out)
    r = shim(["-v", "error", "-i", str(clip), "-c", "copy", str(out)], env, work)
    assert r.returncode != 0 and sha(out) == before and leftovers(work, {out.name}) == []


def test_writing_a_file_over_its_own_input_is_still_refused_by_ffmpeg(work, clip, env):
    out = asset(work, clip)
    before = sha(out)
    r = shim(["-y", "-v", "error", "-i", str(out), "-c", "copy", str(out)], env, work)
    assert r.returncode != 0 and sha(out) == before


def test_a_new_destination_is_written_as_before(work, clip, env):
    out = work / "fresh.mp4"
    r = shim(["-y", "-v", "error", "-i", str(clip), "-t", "1", "-c", "copy", str(out)], env, work)
    assert r.returncode == 0 and probe_streams(out) == ["video", "audio"]
    assert leftovers(work, {out.name}) == []


def test_a_cpu_only_reencode_over_an_existing_file_is_owned_too(work, clip, env):
    """Not just stream copy: no ``-pix_fmt`` keeps the encode on the CPU, still over a production file."""
    out = asset(work, clip)
    before = sha(out)
    bad = shim(["-y", "-v", "error", "-i", str(clip), "-c:v", "libx264", "-crf", "30", str(out),
                "-c", "copy", str(work / "no-such-dir" / "s.mp4")], env, work)
    assert bad.returncode != 0 and sha(out) == before and leftovers(work, {out.name}) == []
    ok = shim(["-y", "-v", "error", "-i", str(clip), "-t", "1", "-c:v", "libx264", "-crf", "30", str(out)], env, work)
    assert ok.returncode == 0 and sha(out) != before and leftovers(work, {out.name}) == []


def test_a_pure_implicit_hls_command_still_runs_as_the_explicit_form_does(work, clip, env):
    """HLS cannot be owned as one path; an unaccelerated HLS command is unchanged (not refused)."""
    r = shim(["-y", "-v", "error", "-i", str(clip), "-c", "copy", "-hls_time", "1",
              "-hls_segment_filename", str(work / "seg_%03d.ts"), str(work / "playlist.m3u8")], env, work)
    assert r.returncode == 0
    assert (work / "playlist.m3u8").is_file() and list(work.glob("seg_*.ts"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_writable_file_in_an_unwritable_directory_is_written_in_place_as_before(work, clip, env):
    out = asset(work, clip)
    os.chmod(work, 0o555)
    try:
        r = shim(["-y", "-v", "error", "-i", str(clip), "-t", "1", "-c", "copy", str(out)], env, work)
    finally:
        os.chmod(work, 0o755)
    assert r.returncode == 0 and probe_streams(out) == ["video", "audio"]
