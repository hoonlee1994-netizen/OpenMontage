"""Data-safety rules of the GPU-first ffmpeg dispatcher's CPU fallback."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.gpu import ffmpeg_exec

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_stage_cmd_rewrites_to_a_sibling_temp_with_same_extension(tmp_path):
    final = str(tmp_path / "out.mp4")
    cmd = ["-y", "-i", "in.mp4", "-c:v", "h264_mediacodec", final]
    staged_cmd, pairs = ffmpeg_exec._stage_cmd(cmd, [final], has_y=True)
    assert len(pairs) == 1 and pairs[0][0] == final
    temp = pairs[0][1]
    assert temp != final
    assert os.path.dirname(temp) == str(tmp_path)
    assert temp.endswith(".mp4")  # same muxer detection
    assert staged_cmd[-1] == temp and final not in staged_cmd


def test_stage_cmd_refuses_existing_file_without_overwrite_flag(tmp_path):
    final = tmp_path / "out.mp4"
    final.write_bytes(b"precious")
    # No -y: ffmpeg would refuse to overwrite; staging must not fake success.
    with pytest.raises(ffmpeg_exec._CannotStage) as exc:
        ffmpeg_exec._stage_cmd(["-i", "in.mp4", str(final)], [str(final)], has_y=False)
    assert exc.value.kind == "overwrite"


def test_stage_cmd_allows_existing_file_with_yes(tmp_path):
    final = tmp_path / "out.mp4"
    final.write_bytes(b"old")
    _cmd, pairs = ffmpeg_exec._stage_cmd(["-y", "-i", "in.mp4", str(final)], [str(final)], has_y=True)
    assert pairs[0][0] == str(final)


def test_stage_cmd_leaves_pipe_targets_alone(tmp_path):
    staged_cmd, pairs = ffmpeg_exec._stage_cmd(["-i", "in.mp4", "pipe:1"], ["pipe:1"], has_y=False)
    assert pairs == [] and staged_cmd[-1] == "pipe:1"


def test_stage_cmd_refuses_a_target_token_it_cannot_find(tmp_path):
    final = str(tmp_path / "out.mp4")
    with pytest.raises(ffmpeg_exec._CannotStage) as exc:
        ffmpeg_exec._stage_cmd(["-y", "-i", "in.mp4", "other.mp4"], [final], has_y=True)
    assert exc.value.kind == "absent"


def test_commit_publishes_atomically_and_discard_leaves_no_trace(tmp_path):
    final = tmp_path / "out.mp4"
    final.write_bytes(b"old")
    _cmd, pairs = ffmpeg_exec._stage_cmd(["-y", "-i", "in.mp4", str(final)], [str(final)], has_y=True)
    temp = pairs[0][1]
    with open(temp, "wb") as fh:
        fh.write(b"new-and-valid")
    assert ffmpeg_exec._commit_outputs(pairs) is None
    assert final.read_bytes() == b"new-and-valid" and not os.path.exists(temp)


def test_discarded_attempt_never_touches_the_final_output(tmp_path):
    final = tmp_path / "user.mp4"
    final.write_bytes(b"asset")
    _cmd, pairs = ffmpeg_exec._stage_cmd(["-y", "-i", "in.mp4", str(final)], [str(final)], has_y=True)
    with open(pairs[0][1], "wb") as fh:
        fh.write(b"partial garbage")
    ffmpeg_exec._discard_outputs(pairs)
    assert final.read_bytes() == b"asset"          # untouched user asset
    assert not os.path.exists(pairs[0][1])         # no leftover temp


def test_each_attempt_uses_a_distinct_temp(tmp_path):
    final = str(tmp_path / "out.mp4")
    cmd = ["-y", "-i", "in.mp4", final]
    first = ffmpeg_exec._stage_cmd(cmd, [final], has_y=True)[1][0][1]
    second = ffmpeg_exec._stage_cmd(cmd, [final], has_y=True)[1][0][1]
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


# --- staging every attempt, not just the hardware one -----------------------

def test_stage_cmd_returns_pairs_for_a_cpu_command(tmp_path):
    final = str(tmp_path / "out.mp4")
    cpu = ["/usr/bin/ffmpeg", "-y", "-i", "in.mp4", "-c:v", "libx264",
           "-pix_fmt", "yuv420p", final]
    staged_cmd, pairs = ffmpeg_exec._stage_cmd(cpu, [final], has_y=True)
    assert staged_cmd[0] == "/usr/bin/ffmpeg"      # the same executable
    assert pairs[0][0] == final and pairs[0][1] != final
    assert staged_cmd[-1] == pairs[0][1]          # writes the temp, not the final
    assert os.path.dirname(pairs[0][1]) == str(tmp_path)


def test_stage_cmd_raises_when_it_cannot_own_the_destination(tmp_path):
    final = str(tmp_path / "out.mp4")
    try:
        ffmpeg_exec._stage_cmd(["-y", "-i", "in.mp4", "other.mp4"], [final], has_y=True)
    except ffmpeg_exec._CannotStage:
        pass
    else:  # pragma: no cover
        raise AssertionError("an unlocatable output must not be staged")


def test_two_attempts_never_share_a_destination(tmp_path):
    final = str(tmp_path / "out.mp4")
    gpu = ffmpeg_exec._stage_cmd(["-y", "in.mp4", final], [final], has_y=True)[1][0][1]
    cpu = ffmpeg_exec._stage_cmd(["-y", "in.mp4", final], [final], has_y=True)[1][0][1]
    assert gpu != cpu and gpu != final and cpu != final


# --- one all-or-nothing publish for the whole output group ------------------

def test_commit_refuses_a_directory_target_before_touching_anything(tmp_path):
    good = tmp_path / "good.mp4"
    good.write_bytes(b"OLD-GOOD")
    blocked = tmp_path / "blocked.mp4"
    blocked.mkdir()
    temp_a = tmp_path / "a.tmp"
    temp_a.write_bytes(b"NEW-A")
    temp_b = tmp_path / "b.tmp"
    temp_b.write_bytes(b"NEW-B")
    err = ffmpeg_exec._commit_outputs([(str(good), str(temp_a)), (str(blocked), str(temp_b))])
    assert err is not None and "directory" in err
    assert good.read_bytes() == b"OLD-GOOD"        # first output not replaced
    assert temp_a.exists() and temp_b.exists()     # nothing was published


def test_commit_refuses_a_missing_output_directory(tmp_path):
    good = tmp_path / "good.mp4"
    good.write_bytes(b"OLD-GOOD")
    temp_a = tmp_path / "a.tmp"
    temp_a.write_bytes(b"NEW-A")
    temp_b = tmp_path / "b.tmp"
    temp_b.write_bytes(b"NEW-B")
    missing = str(tmp_path / "nope" / "second.mp4")
    err = ffmpeg_exec._commit_outputs([(str(good), str(temp_a)), (missing, str(temp_b))])
    assert err is not None and "no such directory" in err
    assert good.read_bytes() == b"OLD-GOOD"


def test_commit_rolls_back_when_a_later_publish_fails(tmp_path, monkeypatch):
    first = tmp_path / "first.mp4"
    first.write_bytes(b"OLD-FIRST")
    second = tmp_path / "second.mp4"
    second.write_bytes(b"OLD-SECOND")
    temp_a = tmp_path / "a.tmp"
    temp_a.write_bytes(b"NEW-FIRST")
    temp_b = tmp_path / "b.tmp"
    temp_b.write_bytes(b"NEW-SECOND")

    real_replace = os.replace

    def flaky(src, dst, *args, **kwargs):
        if str(dst) == str(second):
            raise OSError("injected publish failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky)
    err = ffmpeg_exec._commit_outputs([(str(first), str(temp_a)), (str(second), str(temp_b))])
    assert err is not None and "second.mp4" in err
    assert first.read_bytes() == b"OLD-FIRST"      # already-published output restored
    assert second.read_bytes() == b"OLD-SECOND"
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".om-gpu-")]


def test_commit_aborts_and_restores_when_the_job_was_cancelled(tmp_path):
    first = tmp_path / "first.mp4"
    first.write_bytes(b"OLD-FIRST")
    temp_a = tmp_path / "a.tmp"
    temp_a.write_bytes(b"NEW-FIRST")
    ffmpeg_exec._OWNED["signum"] = signal.SIGTERM
    try:
        err = ffmpeg_exec._commit_outputs([(str(first), str(temp_a))])
    finally:
        ffmpeg_exec._OWNED["signum"] = 0
    assert err is not None and "cancel" in err
    assert first.read_bytes() == b"OLD-FIRST"


def test_commit_keeps_the_preexisting_file_mode(tmp_path):
    final = tmp_path / "out.mp4"
    final.write_bytes(b"OLD")
    os.chmod(final, 0o640)
    temp = tmp_path / "a.tmp"
    temp.write_bytes(b"NEW")
    os.chmod(temp, 0o600)
    assert ffmpeg_exec._commit_outputs([(str(final), str(temp))]) is None
    assert final.read_bytes() == b"NEW"
    assert os.stat(final).st_mode & 0o777 == 0o640


# --- owned subprocess lifecycle --------------------------------------------

def test_owned_child_leads_its_session_and_carries_pdeathsig():
    ffmpeg_exec._load()
    code = ("import ctypes, os, sys\n"
            "buf = ctypes.c_int(0)\n"
            "ctypes.CDLL(None).prctl(2, ctypes.byref(buf))\n"
            "sys.stderr.write('%d %d %d\\n' % (os.getpid(), os.getsid(0), buf.value))\n")
    rc, err = ffmpeg_exec._run_owned([sys.executable, "-c", code], dict(os.environ),
                                     capture_stderr=True)
    assert rc == 0
    pid, sid, pdeathsig = (int(x) for x in err.decode().split())
    assert sid == pid                            # own process group: killpg stops it
    assert pdeathsig == int(signal.SIGKILL)      # dies with the dispatcher


def _alive(pid):
    try:
        return "State:\tZ" not in Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return False


def test_owned_child_is_killed_when_its_dispatcher_dies(tmp_path):
    """A real CPU-fallback-shaped child must not outlive the dispatcher."""
    pidfile = tmp_path / "child.pid"
    script = (
        "import os, sys\n"
        "from lib.gpu import ffmpeg_exec as f\n"
        "f._load()\n"
        "f._install_cancel_handlers()\n"
        "f._run_owned(['/bin/sh', '-c', 'echo $$ > ' + sys.argv[1] + '; exec sleep 30'],\n"
        "            dict(os.environ), capture_stderr=True)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    dispatcher = subprocess.Popen([sys.executable, "-c", script, str(pidfile)],
                                  env=env, cwd=str(REPO_ROOT),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    child = None
    try:
        for _ in range(500):
            if pidfile.exists() and pidfile.read_text().strip():
                child = int(pidfile.read_text().strip())
                break
            time.sleep(0.02)
        assert child is not None, "the owned child never started"
        assert _alive(child)
        os.kill(dispatcher.pid, signal.SIGKILL)   # no handler in the dispatcher can run
        dispatcher.wait(timeout=10)
        for _ in range(300):
            if not _alive(child):
                break
            time.sleep(0.02)
        assert not _alive(child), "the owned child outlived its dispatcher"
    finally:
        if child is not None and _alive(child):
            os.kill(child, signal.SIGKILL)
        if dispatcher.poll() is None:
            dispatcher.kill()


# --- the recovery chain end to end (hardware fails, CPU fallback runs) ------

class _Slot:
    def __enter__(self):
        return "slot"

    def __exit__(self, *_exc):
        return False


def _wire_dispatch(monkeypatch, targets, *, gpu_rc=1, cpu_rc=0,
                   gpu_writes=True, cpu_writes=True, yes=True,
                   reads_stdin=False, writes_stdout=False, argv=None):
    """Point dispatch at a fake capability layer whose hardware path fails.

    ``targets`` names the video outputs (one GPU plan each); ``argv`` overrides
    the command line for mixed-output and streaming cases.
    """
    from lib.gpu import ffmpeg_args, oop, runtime, telemetry

    calls = {}
    plans = [SimpleNamespace(target=str(t), action="gpu", hw_codec="h264", reason="")
             for t in targets]
    if argv is None:
        argv = (["-y"] if yes else []) + ["-i", "in.mp4"]
        for t in targets:
            argv += ["-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(t)]
    else:
        argv = list(argv)
    gpu_argv = [a.replace("libx264", "h264_mediacodec") for a in argv]

    def write_temps(cmd, payload):
        for arg in cmd:
            if isinstance(arg, str) and os.path.basename(arg).startswith(".om-gpu-"):
                with open(arg, "wb") as fh:
                    fh.write(payload)

    monkeypatch.setattr(ffmpeg_exec, "real_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(ffmpeg_exec, "_cpu_env", lambda: dict(os.environ))
    monkeypatch.setattr(ffmpeg_exec, "_validate", lambda outputs: None)
    monkeypatch.setattr(ffmpeg_exec, "_probe_fps", lambda path: None)
    monkeypatch.setattr(ffmpeg_exec, "_reference_semantics", lambda path: "modern")
    monkeypatch.setattr(ffmpeg_args, "translate",
                        lambda a, **kw: (list(gpu_argv), list(plans)))
    monkeypatch.setattr(ffmpeg_args, "reads_stdin", lambda a: reads_stdin)
    monkeypatch.setattr(ffmpeg_args, "writes_stdout", lambda a: writes_stdout)
    monkeypatch.setattr(ffmpeg_args, "renders_text", lambda a: False)
    monkeypatch.setattr(runtime, "capabilities",
                        lambda: {"ok": True, "hw_encoders": {"h264": {}}})
    monkeypatch.setattr(runtime, "hw_encoder",
                        lambda codec: {"component": "c2.mtk.avc.encoder", "max_instances": 1})
    monkeypatch.setattr(runtime, "healthy", lambda backend: True)
    monkeypatch.setattr(runtime, "active_encoder_jobs", lambda kind: 0)
    monkeypatch.setattr(runtime, "codec_slot", lambda *a, **k: _Slot())
    monkeypatch.setattr(runtime, "native_ffmpeg", lambda: "/data/native/ffmpeg")
    monkeypatch.setattr(runtime, "native_env", lambda: {})
    monkeypatch.setattr(runtime, "record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(oop, "warm", lambda: None)
    monkeypatch.setattr(telemetry, "record",
                        lambda ev: calls.setdefault("telemetry", []).append(ev))

    def fake_native(cmd, **kwargs):
        calls["native_cmd"] = list(cmd)
        if gpu_writes:
            write_temps(cmd, b"GPU-OUTPUT")
        return gpu_rc, b"gpu-boom"

    def fake_owned(cmd, env, **kwargs):
        calls["cpu_cmd"] = list(cmd)
        if cpu_writes:
            write_temps(cmd, b"CPU-OUTPUT")
        return cpu_rc, b"cpu-log"

    monkeypatch.setattr(ffmpeg_exec, "_run_native", fake_native)
    monkeypatch.setattr(ffmpeg_exec, "_run_owned", fake_owned)
    return argv, calls


def test_dispatch_cpu_fallback_stages_then_publishes(tmp_path, monkeypatch):
    final = tmp_path / "out.mp4"
    final.write_bytes(b"OLD-ASSET")
    argv, calls = _wire_dispatch(monkeypatch, [final], gpu_rc=1, cpu_rc=0)
    assert ffmpeg_exec.dispatch(argv) == 0
    assert final.read_bytes() == b"CPU-OUTPUT"          # published after success
    assert calls["cpu_cmd"][-1] != str(final)           # via a temp, never direct
    assert os.path.dirname(calls["cpu_cmd"][-1]) == str(tmp_path)
    assert calls["telemetry"][-1]["backend"] == "cpu"
    assert os.listdir(tmp_path) == [final.name]         # no temp or backup left


def test_dispatch_cpu_fallback_failure_leaves_the_old_asset(tmp_path, monkeypatch):
    final = tmp_path / "out.mp4"
    final.write_bytes(b"OLD-ASSET")
    argv, _calls = _wire_dispatch(monkeypatch, [final], gpu_rc=1, cpu_rc=1)
    assert ffmpeg_exec.dispatch(argv) != 0
    assert final.read_bytes() == b"OLD-ASSET"           # never overwritten
    assert os.listdir(tmp_path) == [final.name]


def test_dispatch_does_not_rerun_the_encoder_when_publish_is_refused(tmp_path, monkeypatch):
    good = tmp_path / "good.mp4"
    good.write_bytes(b"OLD-GOOD")
    blocked = tmp_path / "blocked.mp4"
    blocked.mkdir()
    argv, calls = _wire_dispatch(monkeypatch, [good, blocked], gpu_rc=0, cpu_rc=0)
    assert ffmpeg_exec.dispatch(argv) != 0
    assert "cpu_cmd" not in calls                       # no competing fallback writer
    assert good.read_bytes() == b"OLD-GOOD"             # old asset preserved
    assert calls["telemetry"][-1]["backend"] == "gpu-failed"
    assert sorted(os.listdir(tmp_path)) == sorted([good.name, blocked.name])


# --- a fallback never shares a writer with the attempt it follows -----------

def test_traced_fallback_restages_when_the_remote_attempt_errored(tmp_path, monkeypatch):
    from lib.gpu import runtime

    final = tmp_path / "out.mp4"
    first = str(tmp_path / ".om-gpu-first.mp4")

    monkeypatch.setattr(ffmpeg_exec, "_run_native_remote",
                        lambda cmd, capture_stderr: (True, None))  # started, then lost
    monkeypatch.setattr(runtime, "native_env", lambda: {})

    seen = {}

    def fake_owned(cmd, env, **kwargs):
        seen["cmd"] = list(cmd)
        return 0, b""

    monkeypatch.setattr(ffmpeg_exec, "_run_owned", fake_owned)
    restaged = {}

    def restage():
        restaged["called"] = True
        return [str(tmp_path / ".om-gpu-second.mp4"), str(final)]

    code, _err = ffmpeg_exec._run_native([first, str(final)], capture_stderr=True,
                                         allow_remote=True, restage=restage)
    assert code == 0
    assert restaged.get("called") is True
    assert seen["cmd"][0] != first  # the fallback never reuses the remote temp


def test_restage_is_skipped_when_no_remote_run_started(tmp_path, monkeypatch):
    from lib.gpu import runtime

    final = str(tmp_path / "out.mp4")
    monkeypatch.setattr(ffmpeg_exec, "_run_native_remote",
                        lambda cmd, capture_stderr: (False, None))  # worker unavailable
    monkeypatch.setattr(ffmpeg_exec, "_run_owned", lambda cmd, env, **kw: (7, b""))
    monkeypatch.setattr(runtime, "native_env", lambda: {})
    called = []
    code, _err = ffmpeg_exec._run_native([str(tmp_path / ".om-gpu-t.mp4"), final],
                                         capture_stderr=True, allow_remote=True,
                                         restage=lambda: called.append(True))
    assert code == 7 and not called  # nothing ran, so the target is reusable


# --- the complete output surface is owned (not just the GPU video outputs) ---

def _mixed_argv(tmp_path, video_out):
    """GPU video + stream-copy + audio outputs in one command."""
    return ["-y", "-i", "in.mp4",
            "-t", "1", "-c", "copy", str(tmp_path / "copied.mp4"),
            "-t", "1", "-vn", "-c:a", "aac", str(tmp_path / "audio.m4a"),
            "-t", "1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video_out)]


def test_dispatch_stages_every_file_output_of_a_mixed_command(tmp_path, monkeypatch):
    """A failed stream-copy/audio output must not truncate an existing asset."""
    copied = tmp_path / "copied.mp4"
    copied.write_bytes(b"OLD-COPY")                     # the asset at risk
    video = tmp_path / "video.mp4"
    argv, calls = _wire_dispatch(monkeypatch, [video], gpu_rc=1, cpu_rc=1,
                                 argv=_mixed_argv(tmp_path, video))
    assert ffmpeg_exec.dispatch(argv) != 0
    assert copied.read_bytes() == b"OLD-COPY"           # unchanged, not truncated
    # Every output went to a temp, none of the three finals was written directly.
    for final in (copied, tmp_path / "audio.m4a", video):
        assert str(final) not in calls["native_cmd"]
        assert str(final) not in calls["cpu_cmd"]
    assert os.listdir(tmp_path) == [copied.name]        # no temps or backups left


def test_dispatch_publishes_every_file_output_of_a_mixed_command(tmp_path, monkeypatch):
    copied = tmp_path / "copied.mp4"
    copied.write_bytes(b"OLD-COPY")
    video = tmp_path / "video.mp4"
    argv, _calls = _wire_dispatch(monkeypatch, [video], gpu_rc=1, cpu_rc=0,
                                  argv=_mixed_argv(tmp_path, video))
    assert ffmpeg_exec.dispatch(argv) == 0
    for final in (copied, tmp_path / "audio.m4a", video):
        assert final.read_bytes() == b"CPU-OUTPUT"      # all published together
    assert sorted(p.name for p in tmp_path.iterdir()) == ["audio.m4a", "copied.mp4", "video.mp4"]


def test_dispatch_refuses_a_pattern_output_before_running_anything(tmp_path, monkeypatch):
    """A numbered pattern cannot be owned, so nothing may run at all."""
    video = tmp_path / "out.mp4"
    argv, calls = _wire_dispatch(monkeypatch, [video], argv=[
        "-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / "frame_%04d.png")])
    assert ffmpeg_exec.dispatch(argv) == 78
    assert "native_cmd" not in calls and "cpu_cmd" not in calls
    assert os.listdir(tmp_path) == []                   # nothing written


def test_dispatch_refuses_a_multi_file_muxer_output(tmp_path, monkeypatch):
    video = tmp_path / "out.mp4"
    argv, calls = _wire_dispatch(monkeypatch, [video], argv=[
        "-y", "-i", "in.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "hls", str(tmp_path / "seg.m3u8")])
    assert ffmpeg_exec.dispatch(argv) == 78
    assert "native_cmd" not in calls and "cpu_cmd" not in calls
    assert os.listdir(tmp_path) == []


def test_dispatch_refuses_when_a_file_output_cannot_be_located(tmp_path, monkeypatch):
    final = tmp_path / "out.mp4"
    final.write_bytes(b"OLD-ASSET")
    argv, calls = _wire_dispatch(monkeypatch, [final], gpu_rc=1, cpu_rc=0)
    monkeypatch.setattr(ffmpeg_exec, "_locate_output", lambda cmd, target: None)
    assert ffmpeg_exec.dispatch(argv) == 78
    assert "native_cmd" not in calls and "cpu_cmd" not in calls
    assert final.read_bytes() == b"OLD-ASSET"


def test_dispatch_execs_directly_when_ffmpeg_itself_refuses_the_overwrite(tmp_path, monkeypatch):
    """No -y over an existing file: ffmpeg's own refuse semantics are preserved."""
    final = tmp_path / "out.mp4"
    final.write_bytes(b"OLD-ASSET")
    argv, calls = _wire_dispatch(monkeypatch, [final], gpu_rc=1, cpu_rc=0, yes=False)
    execs = []

    def fake_exec(cmd, env):
        execs.append(list(cmd))
        raise SystemExit(0)          # execve never returns

    monkeypatch.setattr(ffmpeg_exec, "_exec", fake_exec)
    try:
        ffmpeg_exec.dispatch(argv)
    except SystemExit:
        pass
    assert execs and execs[0][0] == "/usr/bin/ffmpeg"
    assert str(final) in execs[0]
    assert "native_cmd" not in calls and "cpu_cmd" not in calls  # no probe was started
    assert final.read_bytes() == b"OLD-ASSET"


# --- a streaming command still owns its file outputs ------------------------

def test_streaming_command_stages_its_file_output_and_keeps_pipe_semantics(tmp_path, monkeypatch):
    final = tmp_path / "out.mp4"
    final.write_bytes(b"OLD-ASSET")
    argv, calls = _wire_dispatch(monkeypatch, [final], gpu_rc=1, cpu_rc=0, writes_stdout=True)
    assert ffmpeg_exec.dispatch(argv) == 1                  # the hardware run failed
    assert "cpu_cmd" not in calls                           # a consumed pipe has no fallback
    assert calls["native_cmd"][-1] != str(final)            # staged, never written directly
    assert final.read_bytes() == b"OLD-ASSET"               # the asset survived
    assert os.listdir(tmp_path) == [final.name]             # no temp left behind


def test_streaming_command_publishes_its_file_output_on_success(tmp_path, monkeypatch):
    final = tmp_path / "out.mp4"
    final.write_bytes(b"OLD-ASSET")
    argv, calls = _wire_dispatch(monkeypatch, [final], gpu_rc=0, gpu_writes=True,
                                 writes_stdout=True)
    assert ffmpeg_exec.dispatch(argv) == 0
    assert final.read_bytes() == b"GPU-OUTPUT"
    assert os.listdir(tmp_path) == [final.name]


# --- validation strength follows the output's media type ---------------------

def _stub_ffprobe(monkeypatch, payload):
    """Canned ffprobe that mirrors ``-select_streams v:0`` filtering."""
    def run(cmd, **kwargs):
        streams = payload.get("streams") or []
        if "v:0" in cmd:
            streams = [s for s in streams if s.get("codec_type") == "video"]
        return SimpleNamespace(stdout=json.dumps({"streams": streams}))

    monkeypatch.setattr(ffmpeg_exec, "Path", Path)
    monkeypatch.setattr(ffmpeg_exec, "json", json)
    monkeypatch.setattr(ffmpeg_exec, "_cpu_env", lambda: {})
    monkeypatch.setattr(ffmpeg_exec, "subprocess", SimpleNamespace(run=run))


def test_validate_accepts_an_audio_only_output_for_the_media_kind(tmp_path, monkeypatch):
    f = tmp_path / "audio.m4a"
    f.write_bytes(b"x")
    _stub_ffprobe(monkeypatch, {"streams": [{"codec_type": "audio", "codec_name": "aac"}]})
    assert ffmpeg_exec._validate([(str(f), "media")]) is None


def test_validate_still_requires_a_video_stream_for_a_video_encode(tmp_path, monkeypatch):
    f = tmp_path / "out.mp4"
    f.write_bytes(b"x")
    _stub_ffprobe(monkeypatch, {"streams": [{"codec_type": "audio", "codec_name": "aac"}]})
    assert "no video stream" in ffmpeg_exec._validate([(str(f), "video")])


def test_validate_rejects_a_media_file_with_no_stream(tmp_path, monkeypatch):
    f = tmp_path / "out.mp4"
    f.write_bytes(b"x")
    _stub_ffprobe(monkeypatch, {"streams": []})
    assert "no media stream" in ffmpeg_exec._validate([(str(f), "media")])


def test_validate_never_probes_a_plain_file_output(tmp_path, monkeypatch):
    f = tmp_path / "checksums.md5"
    f.write_bytes(b"abc")

    def _boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("a text/checksum output must not be ffprobed")

    monkeypatch.setattr(ffmpeg_exec, "Path", Path)
    monkeypatch.setattr(ffmpeg_exec, "subprocess", SimpleNamespace(run=_boom))
    assert ffmpeg_exec._validate([(str(f), "file")]) is None


def test_validate_rejects_an_empty_output(tmp_path):
    ffmpeg_exec._load()
    f = tmp_path / "out.mp4"
    f.write_bytes(b"")
    assert "missing or empty" in ffmpeg_exec._validate([(str(f), "media")])
