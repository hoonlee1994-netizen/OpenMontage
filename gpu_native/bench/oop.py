"""Run a command OUTSIDE PRoot, through the Termux app's TermuxService.

Why this is needed
------------------
PRoot 5.1 accelerates tracing with a seccomp filter (``Seccomp: 2``) that
returns ``SECCOMP_RET_TRACE`` for every syscall whose paths it translates.
When no tracer is attached that becomes ``ENOSYS``, and the filter cannot be
removed once installed (seccomp filters only ever stack).  So a traced
process, or any child it forks, **cannot** detach itself, and ``CLONE_UNTRACED``
does not help: the escaped child would answer ``ENOSYS`` to ``openat``/``execve``.

The only proot-free channel on the device is a process forked by the Termux
app, whose ancestor chain never contained the tracer.  ``am startservice``
with ``com.termux.service.action.service_execute`` makes ``TermuxService``
fork exactly such a process (verified: ``TracerPid: 0``, ``PPID`` = the app).

Two namespaces
--------------
The guest (PRoot) and host (Android) filesystems differ.  Inside PRoot the
Debian rootfs is ``/``; on the host it lives under ``<rootfs>``.  A few
prefixes are identity binds (``/data/data/com.termux/files/home``,
``/data/data/com.termux/files/usr``); everything else under ``/`` is
``<rootfs>/...``.  :func:`guest_to_host` implements that mapping.

API
---
``run(cmd, cwd=..., env=..., timeout=...) -> Result``

The command is written to a script on the (identity-bound) host path and the
script is executed, so no argument ever has to survive ``am``'s comma-split
``--esa`` encoding.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

TERMUX_PREFIX = "/data/data/com.termux/files/usr"
TERMUX_HOME = "/data/data/com.termux/files/home"
DEFAULT_ROOTFS = TERMUX_PREFIX + "/var/lib/proot-distro/containers/debian/rootfs"
DEFAULT_WORKER_HOME = TERMUX_HOME + "/.openmontage-gpu/state/oop"

# Prefixes bound 1:1 into the guest, i.e. the same absolute path on both sides.
_IDENTITY_PREFIXES = (
    TERMUX_HOME,
    TERMUX_PREFIX,
    "/data/data/com.termux/cache",
    "/data/app",
    "/apex",
    "/odm",
    "/product",
    "/system",
    "/system_ext",
    "/vendor",
    "/data/dalvik-cache",
    "/data/misc/apexdata/com.android.art/dalvik-cache",
    "/linkerconfig",
)


def rootfs() -> str:
    return os.environ.get("OM_GPU_ROOTFS") or DEFAULT_ROOTFS


def guest_to_host(path: str, root: "str | None" = None) -> str:
    """Map a PRoot guest path onto the Android host filesystem."""
    if not path.startswith("/"):
        return path
    root = root or rootfs()
    for p in _IDENTITY_PREFIXES:
        if path == p or path.startswith(p + "/"):
            return path
    # /sdcard and friends are bind-mounted from /storage/self/primary.
    for p in ("/sdcard", "/mnt/sdcard", "/storage/emulated/0"):
        if path == p or path.startswith(p + "/"):
            return "/storage/self/primary" + path[len(p):]
    return root + path


def host_to_guest(path: str, root: "str | None" = None) -> str:
    """Inverse of :func:`guest_to_host` (best effort)."""
    root = root or rootfs()
    if path.startswith("/storage/self/primary"):
        return "/sdcard" + path[len("/storage/self/primary"):]
    if path.startswith(root + "/"):
        return path[len(root):]
    return path


class Result:
    def __init__(self, rc: int, stdout: str, stderr: str, wall_s: float, timed_out: bool = False):
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.wall_s = wall_s
        self.timed_out = timed_out

    def __repr__(self) -> str:
        return f"Result(rc={self.rc}, wall_s={self.wall_s:.3f}, timed_out={self.timed_out})"


def _am_startservice(script_host_path: str, workdir_host: str) -> None:
    subprocess.run(
        [
            "am", "startservice", "--user", "0",
            "-n", "com.termux/com.termux.app.TermuxService",
            "-a", "com.termux.service.action.service_execute",
            "-d", "file://" + TERMUX_PREFIX + "/bin/sh",
            "--esa", "com.termux.execute.arguments", "-c," + script_host_path,
            "--ez", "com.termux.execute.background", "true",
            "--es", "com.termux.execute.workdir", workdir_host,
        ],
        capture_output=True, text=True, timeout=60,
    )


def run(cmd: "list[str] | str", *, cwd: "str | None" = None, env: "dict | None" = None,
        timeout: float = 900.0, extra_script: str = "") -> Result:
    """Run ``cmd`` in an app-spawned (proot-free) process; wait for completion.

    ``cwd`` is a *guest* path (translated to the host); ``cmd`` arguments are
    passed through unchanged, so use guest paths for files the guest names.
    """
    work = Path(os.environ.get("OM_GPU_OOP_DIR") or DEFAULT_WORKER_HOME)
    work.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:12]
    out_f = work / f"{tag}.out"
    err_f = work / f"{tag}.err"
    rc_f = work / f"{tag}.rc"
    script = work / f"{tag}.sh"

    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    prefix = ""
    if env:
        prefix = " ".join(f"export {k}={shlex.quote(str(v))};" for k, v in env.items())
    body = (
        "#!/data/data/com.termux/files/usr/bin/sh\n"
        + prefix + "\n"
        + shlex.join(cmd)
        + f" > {out_f} 2> {err_f}; echo $? > {rc_f}\n"
    )
    if extra_script:
        body += extra_script + "\n"
    script.write_text(body)
    script.chmod(0o700)

    workdir_host = guest_to_host(cwd) if cwd else TERMUX_HOME
    os.makedirs(workdir_host, exist_ok=True)

    t0 = time.monotonic()
    _am_startservice(str(script), workdir_host)
    timed_out = True
    while time.monotonic() - t0 < timeout:
        if rc_f.exists():
            timed_out = False
            break
        time.sleep(0.1)
    wall = time.monotonic() - t0
    if timed_out:
        rc = -1
    else:
        try:
            rc = int(rc_f.read_text().strip() or "-1")
        except (OSError, ValueError):
            rc = -1
    stdout = out_f.read_text(errors="replace") if out_f.exists() else ""
    stderr = err_f.read_text(errors="replace") if err_f.exists() else ""
    for f in (out_f, err_f, rc_f, script):
        try:
            f.unlink()
        except OSError:
            pass
    return Result(rc, stdout, stderr, wall, timed_out)


def available() -> bool:
    """True when the TermuxService channel answers."""
    try:
        r = run(["sh", "-c", "echo OOP_OK"], timeout=30)
    except Exception:
        return False
    return r.rc == 0 and "OOP_OK" in r.stdout


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="run a command outside PRoot")
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    argv = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    res = run(argv, cwd=a.cwd, timeout=a.timeout)
    print(res.stdout, end="")
    print(res.stderr, end="", file=__import__("sys").stderr)
    raise SystemExit(res.rc)
