"""GPU-first FFmpeg dispatcher.

Every ``ffmpeg`` invocation made by OpenMontage (tools, Remotion's encoder,
HyperFrames) resolves to ``gpu_native/shims/path/ffmpeg``, which calls
:func:`main`. For each command it:

1. translates CPU video encodes onto the verified MediaCodec hardware encoder
   (``lib.gpu.ffmpeg_args``) and runs them on the native (bionic) FFmpeg;
2. validates the produced files;
3. on any GPU failure, re-runs the *original, unmodified* command on the
   Debian FFmpeg (``auto`` mode) — or fails loudly (``require`` mode);
4. records which backend actually did the work (``lib.gpu.telemetry``).

Commands with nothing to accelerate (stream copy, analysis, audio, images)
are exec'd on the Debian FFmpeg directly, with no overhead beyond the shim.

Commands that read stdin or write stdout cannot be replayed after a partial
run, so they take the GPU path only while the encoder backend is healthy and
are exec'd without fallback.
"""
from __future__ import annotations

import os
import sys

from lib.gpu import ffmpeg_args

# Heavier modules (subprocess, json, the runtime probe, telemetry) are only
# imported once a command actually has something to accelerate: the shim runs
# for every ffmpeg call and Python imports are slow under proot.
json = shutil = signal = subprocess = time = runtime = telemetry = None  # type: ignore
Path = None  # type: ignore
Optional = None  # type: ignore


def _load() -> None:
    global json, shutil, signal, subprocess, time, runtime, telemetry, Path
    if runtime is not None:
        return
    import json as _json
    import shutil as _shutil
    import signal as _signal
    import subprocess as _subprocess
    import time as _time
    from pathlib import Path as _Path

    from lib.gpu import runtime as _runtime
    from lib.gpu import telemetry as _telemetry

    json, shutil, signal, subprocess, time = _json, _shutil, _signal, _subprocess, _time
    runtime, telemetry, Path = _runtime, _telemetry, _Path

_SHIM_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "gpu_native", "shims", "path")

BACKEND = "mediacodec-encoder"

# Whether the last native run went through the PRoot-free worker (telemetry).
OOP_LAST = False


def real_ffmpeg() -> str:
    """The Debian (CPU) ffmpeg, skipping our own shim directory on PATH."""
    explicit = os.environ.get("OM_GPU_REAL_FFMPEG")
    if explicit and os.path.isfile(explicit):
        return explicit
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or os.path.abspath(d) == _SHIM_DIR:
            continue
        cand = os.path.join(d, "ffmpeg")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return "/usr/bin/ffmpeg"


def _cpu_env() -> dict:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                  if os.path.abspath(p) != _SHIM_DIR)
    return env


def _mode() -> str:
    m = (os.environ.get("OM_GPU_MODE") or "").strip().lower()
    if m in ("auto", "cpu", "require"):
        return m
    _load()
    return runtime.mode()


def _installed() -> bool:
    home = os.environ.get("OM_GPU_HOME") or "/data/data/com.termux/files/home/.openmontage-gpu"
    return os.path.isfile(os.path.join(home, "prefix", "bin", "ffmpeg"))


def _exec(cmd: list[str], env: dict[str, str]) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(cmd[0], cmd, env)


def _validate(outputs: list) -> "str | None":
    """Return an error string if any produced file is unusable."""
    for out in outputs:
        p = Path(out)
        if not p.is_file() or p.stat().st_size == 0:
            return f"output missing or empty: {out}"
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,width,height:format=duration",
                 "-of", "json", out],
                capture_output=True, text=True, timeout=60, env=_cpu_env(),
            )
            data = json.loads(proc.stdout or "{}")
        except Exception as exc:
            return f"ffprobe failed on {out}: {exc}"
        streams = data.get("streams") or []
        if not streams:
            return f"no video stream in {out}"
        if not streams[0].get("width"):
            return f"video stream without dimensions in {out}"
    return None


def _snapshot(paths: list) -> dict:
    snap: dict = {}
    for p in paths:
        try:
            st = os.stat(p)
            snap[p] = (st.st_ino, st.st_size, st.st_mtime_ns)
        except OSError:
            snap[p] = None
    return snap


def _remove_partial(paths: list, before: dict) -> None:
    """Delete outputs the failed GPU attempt created or rewrote.

    A file that existed before and is unchanged (e.g. ffmpeg refused to
    overwrite it) is never touched: it may be a user's asset.
    """
    after = _snapshot(paths)
    for p in paths:
        if after.get(p) is not None and after[p] != before.get(p):
            try:
                os.unlink(p)
            except OSError:
                pass


def _reference_semantics(ffmpeg_path: str) -> str:
    """'legacy' for a reference FFmpeg < 7, else 'modern' (see ffmpeg_args.translate).

    Cached per binary (path + mtime) in the runtime state directory.
    """
    _load()
    try:
        st = os.stat(ffmpeg_path)
    except OSError:
        return "legacy"
    key = f"{ffmpeg_path}:{st.st_mtime_ns}"
    cache = runtime.state_dir() / "ffmpeg_versions.json"
    try:
        known = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        known = {}
    major = known.get(key)
    if major is None:
        try:
            out = subprocess.run([ffmpeg_path, "-hide_banner", "-version"], capture_output=True, text=True,
                                 timeout=30, env=_cpu_env()).stdout
            ver = out.split()[2] if out.startswith("ffmpeg version") else ""
            digits = "".join(ch for ch in ver.lstrip("n").split(".")[0] if ch.isdigit())
            major = int(digits) if digits else 0
        except Exception:
            major = 0
        known[key] = major
        try:
            tmp = cache.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(known), encoding="utf-8")
            tmp.replace(cache)
        except OSError:
            pass
    return "modern" if major >= 7 else "legacy"


def _probe_fps(path: str):
    """Frame rate of the first video stream of ``path`` (for GOP checks)."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate,r_frame_rate", "-of", "json", path],
            capture_output=True, text=True, timeout=30, env=_cpu_env(),
        )
        st = (json.loads(proc.stdout or "{}").get("streams") or [{}])[0]
        for key in ("avg_frame_rate", "r_frame_rate"):
            val = ffmpeg_args._rate(st.get(key) or "")
            if val:
                return val
    except Exception:
        pass
    return None


def _set_pdeathsig() -> None:
    # The native ffmpeg must die with its dispatcher (e.g. when a caller's
    # subprocess timeout kills the shim), never linger holding a codec slot.
    try:
        import ctypes

        ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def _run_native_remote(cmd: list[str], *, capture_stderr: bool) -> "tuple[int, bytes] | None":
    """Run the native command in the PRoot-free worker, or None to fall back.

    PRoot traces every native FFmpeg launch, and its path translation of the
    bionic runtime's dynamic linking is pure overhead (measured ~1.0 s per
    launch; see gpu_native/bench/oop_bench.py). The worker is an app-spawned
    process on the other side of PRoot (``lib.gpu.oop``), so the encode runs
    untraced. Any failure — worker down, bad path map, timeout — returns None
    and the caller runs the command under PRoot exactly as before.
    """
    global OOP_LAST
    from lib.gpu import oop

    r = oop.run(cmd, cwd=os.getcwd(), env=runtime.native_env(),
                timeout=float(os.environ.get("OM_GPU_OOP_TIMEOUT_S", "3600")),
                wait_s=float(os.environ.get("OM_GPU_OOP_WAIT_S", "2.5")))
    if r is None or r.error:
        return None
    OOP_LAST = True
    if r.stdout:
        try:
            sys.stdout.buffer.write(r.stdout)
            sys.stdout.buffer.flush()
        except (AttributeError, ValueError, OSError):
            pass
    return r.rc, r.stderr or b""


def _run_native(cmd: list[str], *, capture_stderr: bool, allow_remote: bool = True) -> tuple[int, bytes]:
    if allow_remote:
        remote = _run_native_remote(cmd, capture_stderr=capture_stderr)
        if remote is not None:
            return remote
    proc = subprocess.Popen(
        cmd, env=runtime.native_env(), stdin=None, stdout=None,
        stderr=subprocess.PIPE if capture_stderr else None,
        preexec_fn=_set_pdeathsig,
    )

    def forward(signum, _frame):
        try:
            proc.send_signal(signum)
        except ProcessLookupError:
            pass

    old = {s: signal.signal(s, forward) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        _, err = proc.communicate()
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    return proc.returncode, err or b""


def dispatch(argv: list[str]) -> int:
    """Run one ffmpeg command line (``argv`` excludes the program name)."""
    cpu_cmd = [real_ffmpeg(), *argv]
    mode = _mode()

    if mode == "cpu" or not _installed():
        _exec(cpu_cmd, _cpu_env())

    # Plan optimistically first so commands with nothing to accelerate never
    # pay for (or depend on) the hardware capability probe.
    gpu_argv, plans = ffmpeg_args.translate(argv, available={"h264", "hevc"})
    if gpu_argv is None and not any(p.action == "cpu" for p in plans):
        _exec(cpu_cmd, _cpu_env())  # nothing to accelerate: zero-import fast path
    _load()
    t0 = time.time()
    if gpu_argv is not None:
        caps = runtime.capabilities()
        available = set((caps.get("hw_encoders") or {}).keys()) if caps.get("ok") else set()
        gpu_argv, plans = ffmpeg_args.translate(argv, available=available, probe_fps=_probe_fps,
                                                reference=_reference_semantics(cpu_cmd[0]))
    if gpu_argv is None:
        reasons = sorted({p.reason for p in plans if p.action == "cpu"})
        if reasons:
            # A video encode we could not move to hardware: say so.
            telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "cpu",
                              "fallback_reason": "; ".join(reasons),
                              "output": ",".join(p.target for p in plans)})
            if mode == "require":
                sys.stderr.write(f"[openmontage-gpu] OM_GPU_MODE=require but no hardware path: {reasons}\n")
                return 69
        _exec(cpu_cmd, _cpu_env())

    gpu_plans = [p for p in plans if p.action == "gpu"]
    codec = gpu_plans[0].hw_codec
    enc = runtime.hw_encoder(codec) or {}
    # The PRoot-free worker (lib.gpu.oop) runs in Android's `background`
    # cpuset — cores 0-3 only, ~1.5x slower per core here — so it is only
    # worth using for a job that is alone on the encoder: then PRoot's tracing
    # overhead (measured ~4.4 s on a 30 s 1080p hardware encode) dominates.
    # With several encodes in flight the two effects cancel, so we stay traced.
    try:
        solo = runtime.active_encoder_jobs("hwenc") == 0
    except Exception:
        solo = False
    if solo:
        try:
            from lib.gpu import oop

            oop.warm()  # never blocks; gives the worker the rest of the job as lead time
        except Exception:
            pass
    native_cmd = [str(runtime.native_ffmpeg()), *gpu_argv]
    streaming = ffmpeg_args.reads_stdin(argv) or ffmpeg_args.writes_stdout(argv)
    backend_healthy = runtime.healthy(BACKEND)

    if not backend_healthy and mode == "auto":
        telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "cpu",
                          "fallback_reason": "hardware encoder backend unhealthy (recent failures)",
                          "output": ",".join(p.target for p in gpu_plans)})
        _exec(cpu_cmd, _cpu_env())

    slots = int(os.environ.get("OM_GPU_ENCODER_SLOTS") or min(4, int(enc.get("max_instances") or 4)))
    with runtime.codec_slot("hwenc", slots, wait_s=float(os.environ.get("OM_GPU_SLOT_WAIT_S", "900"))) as slot:
        if slot is None:
            if mode == "require":
                sys.stderr.write("[openmontage-gpu] no hardware encoder slot available\n")
                return 75
            telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "cpu",
                              "fallback_reason": "all hardware encoder slots busy",
                              "output": ",".join(p.target for p in gpu_plans)})
            _exec(cpu_cmd, _cpu_env())

        if streaming:
            # Cannot replay a consumed pipe: run on hardware, no fallback.
            rc, _ = _run_native(native_cmd, capture_stderr=False, allow_remote=False)
            runtime.record_outcome(BACKEND, rc == 0, f"exit {rc}")
            telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "gpu" if rc == 0 else "gpu-failed",
                              "encoder": f"{codec}_mediacodec", "device": enc.get("component"),
                              "streaming": True, "rc": rc, "wall_s": round(time.time() - t0, 3),
                              "output": ",".join(p.target for p in gpu_plans)})
            return rc

        file_outputs = [p.target for p in gpu_plans if not ffmpeg_args.is_pipe(p.target)]
        before = _snapshot(file_outputs)
        rc, err = _run_native(native_cmd, capture_stderr=True, allow_remote=solo)
        problem = f"native ffmpeg exit {rc}" if rc != 0 else _validate(
            [p.target for p in gpu_plans if not ffmpeg_args.is_pipe(p.target)])

    if problem is None:
        runtime.record_outcome(BACKEND, True)
        sys.stderr.buffer.write(err)
        sys.stderr.flush()
        telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "gpu",
                          "encoder": f"{codec}_mediacodec", "device": enc.get("component"),
                          "oop": OOP_LAST,
                          "rc": 0, "wall_s": round(time.time() - t0, 3),
                          "output": ",".join(p.target for p in gpu_plans)})
        return 0

    tail = err.decode("utf-8", "replace").strip().splitlines()[-4:]
    detail = f"{problem}: {' | '.join(tail)}"
    if mode == "require":
        runtime.record_outcome(BACKEND, False, detail)
        sys.stderr.buffer.write(err)
        sys.stderr.write(f"[openmontage-gpu] hardware path failed and OM_GPU_MODE=require: {problem}\n")
        telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "gpu-failed", "fallback_reason": detail,
                          "wall_s": round(time.time() - t0, 3),
                          "output": ",".join(p.target for p in gpu_plans)})
        return rc or 1
    # Remove what the failed GPU attempt wrote so the CPU run starts clean.
    _remove_partial(file_outputs, before)
    proc = subprocess.run(cpu_cmd, env=_cpu_env())
    # Only a failure the CPU path does not share is a hardware-path failure;
    # a bad command (e.g. refusing to overwrite) must not trip the breaker.
    if proc.returncode == 0:
        runtime.record_outcome(BACKEND, False, detail)
    telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "cpu" if proc.returncode == 0 else "cpu-failed",
                      "fallback_reason": detail, "rc": proc.returncode,
                      "wall_s": round(time.time() - t0, 3),
                      "output": ",".join(p.target for p in gpu_plans)})
    return proc.returncode


def main(argv: "list[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return dispatch(argv)
    except Exception as exc:  # the dispatcher must never be the reason a job fails
        sys.stderr.write(f"[openmontage-gpu] dispatcher error ({type(exc).__name__}: {exc}); using CPU ffmpeg\n")
        if _mode() == "require":
            return 70
        _exec([real_ffmpeg(), *argv], _cpu_env())
        return 70
