"""Out-of-PRoot execution of native (bionic) commands.

Inside PRoot every process is traced by the PRoot tracer, so each native
FFmpeg launch pays for PRoot's path translation of its dynamic linking (83
libraries on this runtime) and of every media file syscall.  PRoot 5.1 is
seccomp-accelerated: it installs a filter that returns ``SECCOMP_RET_TRACE``
for the syscalls it translates, and that becomes ``ENOSYS`` when no tracer is
attached.  The filter can never be removed (seccomp filters only stack), so a
traced process — and any child it forks, including with ``CLONE_UNTRACED`` —
cannot detach itself.

The only PRoot-free channel is a process forked by the Termux app, whose
ancestor chain never contained the tracer.  ``am startservice`` with
``com.termux.service.action.service_execute`` makes ``TermuxService`` fork
exactly that.  This module:

* derives the guest→host path map from the *real* PRoot command line
  (``--rootfs`` plus every ``--bind``), so paths keep working across the two
  namespaces without hardcoding this device's layout;
* starts the persistent worker (``gpu_native/worker/om_gpu_worker.py``) once
  through that channel and reuses it over a Unix socket;
* hands it a command line, environment and working directory, and returns the
  exit status and captured output.

Callers must keep their own in-PRoot path as the fallback: this is an
optimisation, never a dependency.
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import struct
import subprocess
import time
from pathlib import Path

TERMUX_PREFIX = "/data/data/com.termux/files/usr"
TERMUX_HOME = "/data/data/com.termux/files/home"
DEFAULT_ROOTFS = TERMUX_PREFIX + "/var/lib/proot-distro/containers/debian/rootfs"
DEFAULT_HOME = TERMUX_HOME + "/.openmontage-gpu"

# Guest roots that may appear *inside* a longer argument (FFmpeg filter
# paths, e.g. ``subtitles='/path/x.srt'``).  Only mapped when the host target
# actually exists, so a wrong guess degrades to "leave it alone".
_EMBED_ROOTS = ("/root", "/tmp", "/home", "/var", "/opt", "/srv", "/etc", "/usr",
                "/mnt", "/sdcard", "/workspace")

_CHUNK = 64 * 1024


# ---------------------------------------------------------------------------
# guest <-> host path map, derived from the live PRoot command line
# ---------------------------------------------------------------------------

def _tracer_pid() -> int:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("TracerPid:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def _binds_from_cmdline(argv: "list[str]") -> "tuple[str | None, list[tuple[str, str]]]":
    rootfs = None
    binds: list[tuple[str, str]] = []
    for i, a in enumerate(argv):
        if a.startswith("--rootfs="):
            rootfs = a.split("=", 1)[1]
        elif a == "--rootfs" and i + 1 < len(argv):
            rootfs = argv[i + 1]
        elif a.startswith("--bind=") or a.startswith("-b="):
            spec = a.split("=", 1)[1]
            if not spec.startswith("/"):
                spec = "/" + spec
            parts = spec.split(":")
            if len(parts) == 1:
                binds.append((parts[0].rstrip("/") or "/", parts[0].rstrip("/") or "/"))
            else:
                host, guest = parts[0], parts[1]
                binds.append((host.rstrip("/") or "/", guest.rstrip("/") or "/"))
    return rootfs, binds


class Namespace:
    """Maps absolute guest paths to host paths (longest prefix wins)."""

    def __init__(self, rootfs: "str | None", binds: "list[tuple[str, str]]"):
        self.rootfs = rootfs or DEFAULT_ROOTFS
        self.binds = sorted(
            [(h, g) for h, g in binds if h and g],
            key=lambda hg: -len(hg[1]),
        )

    def to_host(self, path: str) -> str:
        if not path.startswith("/"):
            return path
        for host, guest in self.binds:
            if path == guest:
                return host
            if path.startswith(guest + "/"):
                return host + path[len(guest):]
        if path.startswith(self.rootfs + "/"):
            return path  # already a host path
        return self.rootfs + path

    def to_host_any(self, text: str) -> str:
        """Map a whole argument, including paths embedded in filter strings."""
        if text.startswith("/"):
            return self.to_host(text)
        if "/" not in text or ("=" not in text and "'" not in text and ":" not in text):
            return text
        # Filter arguments look like ``subtitles='/root/x.srt'`` or
        # ``movie=/root/x.png``; rewrite each guest-root path found.
        def repl(m: "re.Match") -> str:
            guest = m.group(0)
            host = self.to_host(guest)
            return host if os.path.lexists(host) else guest
        pattern = r"(?:" + "|".join(re.escape(r) for r in _EMBED_ROOTS) + r")/[^\s'\",:;\[\]()]*"
        return re.sub(pattern, repl, text)


def namespace() -> Namespace:
    """The live namespace, or an identity map when not running under PRoot."""
    override = os.environ.get("OM_GPU_ROOTFS")
    tracer = _tracer_pid()
    if tracer:
        try:
            with open(f"/proc/{tracer}/cmdline", "rb") as fh:
                argv = [p.decode("utf-8", "replace") for p in fh.read().split(b"\0") if p]
            rootfs, binds = _binds_from_cmdline(argv)
            if override:
                rootfs = override
            return Namespace(rootfs, binds)
        except OSError:
            pass
    return Namespace(override or DEFAULT_ROOTFS, [])


# ---------------------------------------------------------------------------
# worker lifecycle
# ---------------------------------------------------------------------------

def _run_dir() -> Path:
    return Path(os.environ.get("OM_GPU_HOME") or DEFAULT_HOME) / "run"


def socket_path() -> Path:
    return _run_dir() / "worker.sock"


def ready_path() -> Path:
    return _run_dir() / "worker.ready"


def worker_script() -> Path:
    return Path(__file__).resolve().parents[2] / "gpu_native" / "worker" / "om_gpu_worker.py"


def _enabled() -> bool:
    return (os.environ.get("OM_GPU_OOP") or "auto").strip().lower() not in ("0", "off", "false", "no")


def _connect(timeout: float = 5.0) -> "socket.socket | None":
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(socket_path()))
        return s
    except OSError:
        return None


def _spawn() -> bool:
    """Start the worker through the Termux app (one process, no PRoot)."""
    _run_dir().mkdir(parents=True, exist_ok=True)
    for p in (socket_path(), ready_path()):
        try:
            p.unlink()
        except OSError:
            pass
    if not _startservice():
        return False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if socket_path().exists() and _connect(1.0) is not None:
            return True
        time.sleep(0.1)
    return False


def _startservice() -> bool:
    """Ask the Termux app to fork the worker. Returns whether the intent was sent."""
    py = TERMUX_PREFIX + "/bin/python3"
    script = namespace().to_host(str(worker_script()))
    args = f"{script},--sock,{socket_path()},--ready,{ready_path()}"
    try:
        subprocess.run(
            ["am", "startservice", "--user", "0",
             "-n", "com.termux/com.termux.app.TermuxService",
             "-a", "com.termux.service.action.service_execute",
             "-d", "file://" + py,
             "--esa", "com.termux.execute.arguments", args,
             "--ez", "com.termux.execute.background", "true",
             "--es", "com.termux.execute.workdir", TERMUX_HOME],
            capture_output=True, text=True, timeout=60,
        )
        return True
    except Exception:
        return False


SPAWN_COOLDOWN_S = 15.0


def warm() -> None:
    """Fire an app-spawn of the worker if none is live.

    Never blocks on readiness and only issues one ``am startservice`` per
    cooldown window, so it is safe to call on every GPU job — the encode then
    either finds a live worker (and skips PRoot) or runs under PRoot as before.
    """
    if not _enabled():
        return
    s = _connect(0.5)
    if s is not None:
        s.close()
        return
    d = _run_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = d / "worker.spawn"
    now = time.time()
    try:
        if stamp.exists() and now - stamp.stat().st_mtime < SPAWN_COOLDOWN_S:
            return
    except OSError:
        pass
    try:
        tmp = stamp.with_suffix(".tmp")
        tmp.write_text(str(int(now)))
        tmp.replace(stamp)
    except OSError:
        return
    _startservice()


def ensure_worker(wait_s: float = 0.0, spawn: bool = True) -> bool:
    """True when a live worker answers; optionally waits for one to come up."""
    if not _enabled():
        return False
    s = _connect(0.5)
    if s is not None:
        s.close()
        return True
    if not spawn:
        return False
    lock = _run_dir() / "worker.lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        import fcntl

        with open(lock, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            s = _connect(0.5)
            if s is not None:
                s.close()
                return True
            if not _enabled():
                return False
            warm()
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                s = _connect(0.5)
                if s is not None:
                    s.close()
                    return True
                time.sleep(0.1)
            return False
    except Exception:
        return warm() or ensure_worker(0.0, spawn=False)


# ---------------------------------------------------------------------------
# request/response
# ---------------------------------------------------------------------------

class OopResult:
    def __init__(self, rc: int, stdout: bytes, stderr: bytes, wall_ms: int, error: "str | None" = None):
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.wall_ms = wall_ms
        self.error = error


def _call(req: dict, timeout: float) -> "OopResult | None":
    s = _connect(min(timeout, 10.0))
    if s is None:
        return None
    try:
        s.settimeout(timeout)
        body = json.dumps(req).encode("utf-8")
        s.sendall(struct.pack(">I", len(body)) + body)
        head = b""
        while len(head) < 4:
            chunk = s.recv(4 - len(head))
            if not chunk:
                return None
            head += chunk
        (size,) = struct.unpack(">I", head)
        buf = bytearray()
        while len(buf) < size:
            chunk = s.recv(min(_CHUNK, size - len(buf)))
            if not chunk:
                return None
            buf += chunk
        data = json.loads(bytes(buf).decode("utf-8"))
        return OopResult(
            data.get("rc", -1),
            base64.b64decode(data.get("stdout_b64") or ""),
            base64.b64decode(data.get("stderr_b64") or ""),
            int(data.get("wall_ms") or 0),
            data.get("error"),
        )
    except Exception:
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass


def run(argv: "list[str]", *, cwd: "str | None" = None, env: "dict[str, str] | None" = None,
        timeout: float = 900.0, stdin: "bytes | None" = None, wait_s: float = 0.0) -> "OopResult | None":
    """Run ``argv`` proot-free.  Returns None when the worker is unavailable."""
    if not ensure_worker(wait_s):
        return None
    ns = namespace()
    host_argv = [str(a) if not isinstance(a, str) else ns.to_host_any(a) for a in argv]
    host_env = None
    if env is not None:
        host_env = {}
        for k, v in env.items():
            host_env[k] = ns.to_host_any(str(v)) if isinstance(v, str) else str(v)
    host_cwd = ns.to_host(cwd) if cwd else None
    req = {
        "argv": host_argv,
        "cwd": host_cwd,
        "env": host_env,
        "timeout": timeout,
    }
    if stdin is not None:
        req["stdin_b64"] = base64.b64encode(stdin).decode()
    res = _call(req, timeout + 30)
    if res is None and ensure_worker(1.0):
        res = _call(req, timeout + 30)
    return res


def stop() -> None:
    """Stop a running worker (used by tests and by preflight teardown)."""
    s = _connect(2.0)
    if s is None:
        return
    s.close()
    pid_file = ready_path()
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 15)
    except (OSError, ValueError):
        pass
    for p in (socket_path(), ready_path()):
        try:
            p.unlink()
        except OSError:
            pass
