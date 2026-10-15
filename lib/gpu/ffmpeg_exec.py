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

Output ownership: the dispatcher first classifies the command's *complete*
output surface (``lib.gpu.ffmpeg_args.own_outputs``, not only the translated
video outputs). Every file output — GPU-encoded, stream-copied, audio, image or
mixed — is staged per attempt beside its final path and published by one atomic,
rollback-capable transaction, only after the attempt succeeded and its files
validated. A failed, cancelled or orphaned encoder can therefore only ever write
a discarded temp, never a production file, and two attempts never own the same
destination at the same time. Each owned subprocess leads its own session and
carries ``PR_SET_PDEATHSIG``, so it is stopped together with its process group
and cannot outlive the dispatcher. A file output the dispatcher cannot own (a
numbered pattern or a multi-file muxer) makes it refuse the whole command before
anything runs, rather than leave an unowned writer on a production asset. When
staging cannot be guaranteed for an existing output without ``-y``, the original
command is exec'd directly so ffmpeg's own refuse/prompt semantics are preserved.
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

# Cancellation bookkeeping for the subprocesses this dispatcher owns.
# ``proc`` is the active attempt's process-group leader (None between runs);
# ``signum`` is a termination signal observed while an attempt was in flight,
# which forces that attempt to be abandoned instead of published; ``saved``
# holds the pre-existing handlers while our own are installed.
_OWNED = {"proc": None, "signum": 0, "saved": None}


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


def _write_err(data: bytes) -> None:
    """Best-effort passthrough of an encoder's captured stderr."""
    if not data:
        return
    try:
        sys.stderr.buffer.write(data)
        sys.stderr.flush()
    except (AttributeError, ValueError, OSError):
        pass


def _validate(outputs: list) -> "str | None":
    """Return an error string if any produced file is unusable.

    ``outputs`` is a list of ``(path, kind)`` pairs. ``kind`` selects the check
    that matches the output's media type: ``"video"`` requires a video stream
    with dimensions (every output produced by a video encode, hardware or CPU),
    ``"media"`` requires at least one stream (audio, image, stream copy or a
    mixed muxer), and ``"file"`` only requires a non-empty file (text, checksum
    and metadata muxers carry no media stream to probe).
    """
    for path, kind in outputs:
        p = Path(path)
        if not p.is_file() or p.stat().st_size == 0:
            return f"output missing or empty: {path}"
        if kind == "file":
            continue
        probe = ["ffprobe", "-v", "error"]
        if kind == "video":
            probe += ["-select_streams", "v:0"]
        probe += ["-show_entries", "stream=codec_type,codec_name,width,height:format=duration",
                  "-of", "json", path]
        try:
            proc = subprocess.run(probe, capture_output=True, text=True, timeout=60, env=_cpu_env())
            data = json.loads(proc.stdout or "{}")
        except Exception as exc:
            return f"ffprobe failed on {path}: {exc}"
        streams = data.get("streams") or []
        if kind == "video":
            if not streams:
                return f"no video stream in {path}"
            if not streams[0].get("width"):
                return f"video stream without dimensions in {path}"
        elif not streams:
            return f"no media stream in {path}"
    return None


def _staged_media(pairs: list, media: dict) -> list:
    """Pair each staged temp with the validation strength of its destination."""
    return [(temp, media.get(final, "media")) for final, temp in pairs]


def _declared_media(paths: list, media: dict) -> list:
    """Pair file outputs directly with their validation strength (no staging)."""
    return [(path, media.get(path, "media")) for path in paths]


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


def _temp_path(final: str) -> str:
    """A unique sibling of ``final`` that keeps its extension (same muxer)."""
    d, name = os.path.split(final)
    root, ext = os.path.splitext(name)
    token = f".om-gpu-{os.getpid()}-{os.urandom(4).hex()}"
    return os.path.join(d, token + ext) if d else token + ext


class _CannotStage(Exception):
    """The dispatcher cannot take ownership of a required file output.

    ``kind`` distinguishes a destination ffmpeg itself would refuse to write
    (``"overwrite"``: an existing target with no ``-y``) from one that cannot be
    identified in the command line at all (``"absent"``).
    """

    def __init__(self, message: str, kind: str = "absent"):
        super().__init__(message)
        self.kind = kind


def _locate_output(cmd: list, final: str) -> "int | None":
    """Index of ``final`` as an output token in ``cmd`` (outputs trail inputs).

    Exact token match first, then a normalised-path match so a ``./out.mp4``
    spelling still resolves; the search runs right-to-left because outputs come
    after the inputs that may share the same path.
    """
    for i in range(len(cmd) - 1, -1, -1):
        if cmd[i] == final:
            return i
    norm = os.path.normpath(final)
    for i in range(len(cmd) - 1, -1, -1):
        if os.path.normpath(cmd[i]) == norm:
            return i
    return None


def _stage_cmd(cmd: list, targets: list, *, has_y: bool) -> "tuple[list, list]":
    """Redirect every non-pipe ``targets`` entry to a fresh sibling temp.

    Returns ``(staged_cmd, [(final, temp), ...])`` or raises ``_CannotStage``
    when ownership cannot be guaranteed: an existing output without ``-y``
    (ffmpeg's refuse/prompt semantics must be preserved) or a target that does
    not appear as an output token. Callers pass *every* file output of the
    command, so one transaction owns the command's complete output surface.
    """
    staged = list(cmd)
    pairs = []
    for final in targets:
        if ffmpeg_args.is_pipe(final):
            continue
        if not has_y and os.path.exists(final):
            raise _CannotStage(f"{final} exists and the command has no -y", "overwrite")
        idx = _locate_output(staged, final)
        if idx is None:
            raise _CannotStage(f"output token not found in command: {final}", "absent")
        temp = _temp_path(final)
        staged[idx] = temp
        pairs.append((final, temp))
    return staged, pairs


def _unowned_staging(exc: _CannotStage, cpu_cmd: list, outputs: list) -> int:
    """Resolve a destination the dispatcher could not stage.

    When ffmpeg itself would refuse the write (an existing target without
    ``-y``) the original command is exec'd, exactly as it was before staging
    existed: this process *is* the single writer, so ffmpeg's own refuse/prompt
    semantics are preserved. Any other cause means the destination could not be
    identified, which must fail before an unowned writer (or a CPU fallback)
    touches a production asset.
    """
    if exc.kind == "overwrite":
        telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "cpu",
                          "fallback_reason": "output ownership unavailable; running unaccelerated",
                          "output": ",".join(outputs)})
        _exec(cpu_cmd, _cpu_env())  # never returns
    sys.stderr.write(f"[openmontage-gpu] refusing to run: cannot take ownership of {exc}; "
                     "nothing was written\n")
    telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "gpu-failed",
                      "fallback_reason": str(exc), "output": ",".join(outputs)})
    return 78


def _commit_outputs(pairs: list) -> "str | None":
    """Publish validated temps over their finals as one all-or-nothing step.

    Every destination is checked *before* anything is touched, and each
    existing final is first backed by a hard link (same inode), so its bytes
    survive even if this process is killed mid-publish. If any publish then
    fails — or the job was cancelled — every final is restored and the error is
    returned; a partial publication is never reported as success.
    """
    for final, _temp in pairs:
        if os.path.isdir(final):
            return f"could not publish {final}: target is a directory"
        parent = os.path.dirname(final) or "."
        if not os.path.isdir(parent):
            return f"could not publish {final}: no such directory: {parent}"
        if not os.access(parent, os.W_OK):
            return f"could not publish {final}: directory not writable: {parent}"

    backups = []    # (final, backup_path, mode) for every pre-existing final
    published = []  # indices whose temp already reached its final
    current = pairs[0][0] if pairs else ""
    try:
        for final, _temp in pairs:
            if os.path.exists(final):
                bak = _temp_path(final) + ".bak"
                mode = os.stat(final).st_mode & 0o7777
                try:
                    os.link(final, bak)  # old bytes stay reachable at final too
                except OSError:
                    os.replace(final, bak)  # filesystem without hard links
                backups.append((final, bak, mode))
        for index, (final, temp) in enumerate(pairs):
            current = final
            if _cancelled():
                raise OSError("cancelled during publish")
            os.replace(temp, final)
            published.append(index)
            for b_final, _bak, mode in backups:
                if b_final == final:
                    try:
                        os.chmod(final, mode)  # replacing kept -rw-r--r--/exec bits
                    except OSError:
                        pass
                    break
    except OSError as exc:
        published_final = {pairs[index][0] for index in published}
        for final, bak, _mode in backups:
            if final not in published_final and os.path.exists(final):
                # Never replaced, so its original bytes are still in place and
                # the backup link is redundant: drop it instead of restoring.
                try:
                    os.unlink(bak)
                except OSError:
                    pass
                continue
            try:
                os.replace(bak, final)  # put the old bytes back
            except OSError:
                try:
                    os.unlink(bak)
                except OSError:
                    pass
        return f"could not publish {current}: {exc}"
    for _final, bak, _mode in backups:
        try:
            os.unlink(bak)
        except OSError:
            pass
    return None


def _discard_outputs(pairs: list) -> None:
    for _final, temp in pairs:
        try:
            os.unlink(temp)
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


def _forward_to(proc, signum: int) -> None:
    """Signal a child's whole process group, not just the child itself."""
    try:
        os.killpg(proc.pid, signum)
        return
    except OSError:
        pass
    try:
        proc.send_signal(signum)
    except (ProcessLookupError, OSError):
        pass


def _cancelled() -> int:
    """Signal number that arrived while an attempt was in flight, else 0."""
    return _OWNED["signum"]


def _owned_signal(signum, _frame) -> None:
    """Record a cancellation and stop the active encoder group immediately.

    The dispatcher itself is not terminated here: the caller abandons the
    attempt (discarding staged temps instead of publishing them) and then exits
    with this signal, so a cancelled job can never publish an output.
    """
    _OWNED["signum"] = signum
    proc = _OWNED["proc"]
    if proc is not None:
        # First cancellation stops the encoder gracefully; a repeat (e.g. a
        # caller that retries its timeout) escalates so nothing survives.
        if proc.returncode is None and _OWNED.get("stopped") == proc.pid:
            _forward_to(proc, signal.SIGKILL)
        else:
            _forward_to(proc, signal.SIGTERM)
            _OWNED["stopped"] = proc.pid


def _install_cancel_handlers() -> None:
    _load()
    _OWNED["signum"] = 0
    _OWNED["stopped"] = None
    _OWNED["saved"] = {s: signal.signal(s, _owned_signal)
                       for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}


def _restore_cancel_handlers() -> None:
    saved = _OWNED.get("saved")
    if not saved:
        return
    for s, handler in saved.items():
        signal.signal(s, handler)
    _OWNED["saved"] = None


def _die_by_signal(signum: int) -> int:
    """Exit exactly as if ``signum`` had terminated the dispatcher."""
    _restore_cancel_handlers()
    try:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    except (OSError, ValueError):
        pass
    return 128 + signum


def _run_owned(cmd: list[str], env: dict, *, capture_stderr: bool) -> tuple[int, bytes]:
    """Run ``cmd`` as a process group owned by this dispatcher.

    The child leads its own session (so the encoder and anything it spawns can
    be stopped together) and carries ``PR_SET_PDEATHSIG`` (so it dies with the
    dispatcher even on SIGKILL or a tool-level timeout). A cancellation signal
    reaching the dispatcher is forwarded to the group by the installed handler;
    the caller sees it through :func:`_cancelled` and must abandon the attempt
    rather than publish it.
    """
    proc = subprocess.Popen(
        cmd, env=env, stdin=None, stdout=None,
        stderr=subprocess.PIPE if capture_stderr else None,
        preexec_fn=_set_pdeathsig,
        start_new_session=True,
    )
    _OWNED["proc"] = proc
    try:
        _, err = proc.communicate()
    finally:
        _OWNED["proc"] = None
    return proc.returncode, err or b""


def _run_native_remote(cmd: list[str], *, capture_stderr: bool) -> "tuple[bool, tuple[int, bytes] | None]":
    """Run the native command in the PRoot-free worker, or fall back to PRoot.

    PRoot traces every native FFmpeg launch, and its path translation of the
    bionic runtime's dynamic linking is pure overhead (measured ~1.0 s per
    launch; see gpu_native/bench/oop_bench.py). The worker is an app-spawned
    process on the other side of PRoot (``lib.gpu.oop``), so the encode runs
    untraced.

    Returns ``(attempted, result)``. ``attempted`` is False when the worker was
    simply unavailable (nothing ran, the caller may reuse its target); it is
    True when a run may have started and then errored, in which case the caller
    must not hand the same output path to its fallback.
    """
    global OOP_LAST
    from lib.gpu import oop

    r = oop.run(cmd, cwd=os.getcwd(), env=runtime.native_env(),
                timeout=float(os.environ.get("OM_GPU_OOP_TIMEOUT_S", "3600")),
                wait_s=float(os.environ.get("OM_GPU_OOP_WAIT_S", "2.5")))
    if r is None:
        return False, None
    if r.error:
        return True, None
    OOP_LAST = True
    if r.stdout:
        try:
            sys.stdout.buffer.write(r.stdout)
            sys.stdout.buffer.flush()
        except (AttributeError, ValueError, OSError):
            pass
    return True, (r.rc, r.stderr or b"")


def _run_native(cmd: list[str], *, capture_stderr: bool, allow_remote: bool = True,
                restage=None) -> tuple[int, bytes]:
    if allow_remote:
        attempted, remote = _run_native_remote(cmd, capture_stderr=capture_stderr)
        if remote is not None:
            return remote
        if attempted and restage is not None:
            # A remote run may have started and then lost its connection; the
            # traced fallback must write a *different* temp so two writers can
            # never share one output path.
            cmd = restage()
    return _run_owned(cmd, runtime.native_env(), capture_stderr=capture_stderr)


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
    # Text that is rasterised with the system font stack (subtitles, ASS,
    # drawtext) must resolve the same fonts as the Debian tools. The PRoot-free
    # worker sees the Android host namespace, where Debian's /usr/share/fonts
    # and /etc/fonts are absent, so libass would emit missing-glyph boxes while
    # still exiting 0. Keep those commands on the traced native path, which
    # has the Debian font configuration and still hardware-encodes.
    text_render = ffmpeg_args.renders_text(argv)
    backend_healthy = runtime.healthy(BACKEND)

    if not backend_healthy and mode == "auto":
        telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "cpu",
                          "fallback_reason": "hardware encoder backend unhealthy (recent failures)",
                          "output": ",".join(p.target for p in gpu_plans)})
        _exec(cpu_cmd, _cpu_env())

    slots = int(os.environ.get("OM_GPU_ENCODER_SLOTS") or min(4, int(enc.get("max_instances") or 4)))
    # From here on the dispatcher owns subprocesses and is the only thing that
    # may publish outputs, so it must see a cancellation before it commits:
    # the handlers below stop the active encoder group and mark the attempt
    # abandoned rather than letting it publish.
    _install_cancel_handlers()
    try:
        with runtime.codec_slot("hwenc", slots, wait_s=float(os.environ.get("OM_GPU_SLOT_WAIT_S", "900"))) as slot:
            if slot is None:
                if mode == "require":
                    sys.stderr.write("[openmontage-gpu] no hardware encoder slot available\n")
                    return 75
                telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "cpu",
                                  "fallback_reason": "all hardware encoder slots busy",
                                  "output": ",".join(p.target for p in gpu_plans)})
                _exec(cpu_cmd, _cpu_env())

            # The complete output contract of the command, not only the
            # translated video outputs: every file the command writes must be
            # owned by an attempt, or the command must not run at all. An
            # output the dispatcher cannot own (a numbered pattern or a
            # multi-file muxer) is refused *before* anything runs, never left to
            # an unowned writer.
            ownership = ffmpeg_args.own_outputs(argv)
            unownable = [o for o in ownership if o.kind == "unsafe"]
            if unownable:
                detail = "; ".join(f"{o.target} ({o.reason})" for o in unownable)
                sys.stderr.write("[openmontage-gpu] refusing to run: cannot take ownership of every "
                                 f"file output ({detail}); nothing was written\n")
                telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "gpu-failed",
                                  "fallback_reason": f"unownable file output: {detail}",
                                  "output": ",".join(o.target for o in unownable)})
                return 78
            file_outs = [o for o in ownership if o.kind == "file"]
            file_outputs = [o.target for o in file_outs]
            media = {o.target: o.media for o in file_outs}

            if streaming:
                # A consumed pipe cannot be replayed, so the hardware run has no
                # fallback. Its file outputs are still staged and published, so a
                # failed or cancelled run cannot leave a half-written production
                # file behind; the pipes themselves are never staged.
                try:
                    run_cmd, staged_pairs = _stage_cmd(native_cmd, file_outputs,
                                                       has_y="-y" in native_cmd)
                except _CannotStage as exc:
                    return _unowned_staging(exc, cpu_cmd, file_outputs)
                rc, _ = _run_native(run_cmd, capture_stderr=False, allow_remote=False)
                if _cancelled():
                    _discard_outputs(staged_pairs)
                    return _die_by_signal(_cancelled())
                if rc == 0 and staged_pairs:
                    problem = _validate(_staged_media(staged_pairs, media))
                    if problem is None:
                        problem = _commit_outputs(staged_pairs)
                    if problem is not None:
                        _discard_outputs(staged_pairs)
                        sys.stderr.write(f"[openmontage-gpu] streaming output could not be published: "
                                         f"{problem}\n")
                        rc = 1
                else:
                    _discard_outputs(staged_pairs)
                runtime.record_outcome(BACKEND, rc == 0, f"exit {rc}")
                telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "gpu" if rc == 0 else "gpu-failed",
                                  "encoder": f"{codec}_mediacodec", "device": enc.get("component"),
                                  "streaming": True, "rc": rc, "wall_s": round(time.time() - t0, 3),
                                  "output": ",".join(p.target for p in gpu_plans)})
                return rc

            before = _snapshot(file_outputs)
            if file_outputs:
                # Stage every file output to per-attempt temps: a failed or
                # orphaned encoder writes temp paths, never the user's finals,
                # and a retry uses fresh temps rather than racing a live writer.
                try:
                    run_cmd, staged_pairs = _stage_cmd(native_cmd, file_outputs,
                                                       has_y="-y" in native_cmd)
                except _CannotStage as exc:
                    return _unowned_staging(exc, cpu_cmd, file_outputs)
            else:
                # Nothing on disk to protect (pipe outputs only).
                run_cmd, staged_pairs = native_cmd, []

            def _fresh_stage():
                """Re-point the traced fallback at brand-new temps."""
                for i, (final, temp) in enumerate(staged_pairs):
                    new_temp = _temp_path(final)
                    idx = _locate_output(run_cmd, temp)
                    if idx is not None:
                        run_cmd[idx] = new_temp
                    try:
                        os.unlink(temp)
                    except OSError:
                        pass
                    staged_pairs[i] = (final, new_temp)
                return run_cmd

            rc, err = _run_native(run_cmd, capture_stderr=True,
                                  allow_remote=solo and not text_render,
                                  restage=_fresh_stage if staged_pairs else None)
            if _cancelled():
                _discard_outputs(staged_pairs)
                return _die_by_signal(_cancelled())
            produced = _staged_media(staged_pairs, media) if staged_pairs \
                else _declared_media(file_outputs, media)
            problem = f"native ffmpeg exit {rc}" if rc != 0 else _validate(produced)
            publish_failed = False
            if problem is None and staged_pairs:
                problem = _commit_outputs(staged_pairs)
                publish_failed = problem is not None
            if problem is not None:
                _discard_outputs(staged_pairs)

        if problem is None:
            runtime.record_outcome(BACKEND, True)
            _write_err(err)
            telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "gpu",
                              "encoder": f"{codec}_mediacodec", "device": enc.get("component"),
                              "oop": OOP_LAST, "text_render": text_render,
                              "rc": 0, "wall_s": round(time.time() - t0, 3),
                              "output": ",".join(p.target for p in gpu_plans)})
            return 0

        tail = err.decode("utf-8", "replace").strip().splitlines()[-4:]
        detail = f"{problem}: {' | '.join(tail)}"
        if mode == "require":
            runtime.record_outcome(BACKEND, False, detail)
            _write_err(err)
            sys.stderr.write(f"[openmontage-gpu] hardware path failed and OM_GPU_MODE=require: {problem}\n")
            telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "gpu-failed", "fallback_reason": detail,
                              "wall_s": round(time.time() - t0, 3),
                              "output": ",".join(p.target for p in gpu_plans)})
            return rc or 1

        if publish_failed:
            # The encoders produced valid output; the destination refused it and
            # the transaction restored every pre-existing file. A CPU re-run
            # cannot help and must not touch the destination again.
            runtime.record_outcome(BACKEND, False, detail)
            sys.stderr.write(f"[openmontage-gpu] could not publish output; existing files left untouched: "
                             f"{problem}\n")
            telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "gpu-failed", "fallback_reason": detail,
                              "wall_s": round(time.time() - t0, 3),
                              "output": ",".join(p.target for p in gpu_plans)})
            return rc or 1

        # Remove what the failed GPU attempt wrote so the CPU run starts clean.
        _remove_partial(file_outputs, before)
        # The fallback obeys exactly the same ownership contract as the hardware
        # attempt, over the same complete set of file outputs: its own fresh
        # temps, its own owned process group, and a publish only after it
        # completes and validates.
        if file_outputs:
            try:
                cpu_run, cpu_pairs = _stage_cmd(cpu_cmd, file_outputs, has_y="-y" in cpu_cmd)
            except _CannotStage as exc:
                return _unowned_staging(exc, cpu_cmd, file_outputs)
        else:
            cpu_run, cpu_pairs = cpu_cmd, []
        rc2, err2 = _run_owned(cpu_run, _cpu_env(), capture_stderr=True)
        if _cancelled():
            _discard_outputs(cpu_pairs)
            return _die_by_signal(_cancelled())
        cpu_problem = f"cpu ffmpeg exit {rc2}" if rc2 != 0 else None
        if cpu_problem is None:
            cpu_problem = _validate(_staged_media(cpu_pairs, media) if cpu_pairs
                                    else _declared_media(file_outputs, media))
        if cpu_problem is None and cpu_pairs:
            cpu_problem = _commit_outputs(cpu_pairs)
        if cpu_problem is not None:
            _discard_outputs(cpu_pairs)
            _write_err(err2)
            sys.stderr.write(f"[openmontage-gpu] cpu fallback produced no usable output: {cpu_problem}\n")
            telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "cpu-failed",
                              "fallback_reason": f"{detail}; {cpu_problem}", "rc": rc2,
                              "wall_s": round(time.time() - t0, 3),
                              "output": ",".join(p.target for p in gpu_plans)})
            return rc2 or 1
        # Only a failure the CPU path does not share is a hardware-path failure;
        # a bad command (e.g. refusing to overwrite) must not trip the breaker.
        runtime.record_outcome(BACKEND, False, detail)
        _write_err(err2)
        telemetry.record({"kind": "job", "op": "ffmpeg", "backend": "cpu",
                          "fallback_reason": detail, "rc": 0,
                          "wall_s": round(time.time() - t0, 3),
                          "output": ",".join(p.target for p in gpu_plans)})
        return 0
    finally:
        _restore_cancel_handlers()


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
