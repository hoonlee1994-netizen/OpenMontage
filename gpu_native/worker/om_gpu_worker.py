#!/data/data/com.termux/files/usr/bin/python3
"""OpenMontage GPU worker: the PRoot-free end of the native execution path.

This process is started *by the Termux app* (``am startservice`` with
``com.termux.service.action.service_execute``), so its ancestor chain never
contained the PRoot tracer and ``/proc/self/status`` reports ``TracerPid: 0``.
A traced process cannot reach that state by itself: PRoot 5.1's seccomp filter
answers ``ENOSYS`` to path-translating syscalls once no tracer is attached.

It listens on a Unix socket in the (identity-bound) ``$HOME`` and runs native
bionic commands on request, returning exit status and captured output.  The
client half is ``lib/gpu/oop.py``.

Protocol: 4-byte big-endian length + UTF-8 JSON, one request/response per
connection.  Request keys: argv, cwd, env, timeout, stdin_b64.  Response keys:
rc, stdout_b64, stderr_b64, wall_ms, error.

The worker holds no privileges and only runs the argv it is handed; the client
always validates the result and keeps the in-PRoot path as its fallback.
"""
from __future__ import annotations

import base64
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

MAX_FRAME = 64 * 1024 * 1024


def _recv_exact(conn: socket.socket, n: int) -> "bytes | None":
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _read_request(conn: socket.socket) -> "dict | None":
    head = _recv_exact(conn, 4)
    if head is None:
        return None
    (size,) = struct.unpack(">I", head)
    if size > MAX_FRAME:
        raise ValueError(f"frame too large: {size}")
    body = _recv_exact(conn, size)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def _send_response(conn: socket.socket, obj: dict) -> None:
    body = json.dumps(obj).encode("utf-8")
    conn.sendall(struct.pack(">I", len(body)) + body)


def _handle(conn: socket.socket) -> None:
    try:
        req = _read_request(conn)
        if req is None:
            return
        argv = list(req["argv"])
        cwd = req.get("cwd") or None
        env = req.get("env") or None
        timeout = float(req.get("timeout") or 600.0)
        stdin = base64.b64decode(req["stdin_b64"]) if req.get("stdin_b64") else None
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            # If the client (the shim) dies, the socket closes: kill the child
            # so it cannot outlive the codec slot its caller was holding.
            def watch() -> None:
                try:
                    while True:
                        if not conn.recv(1):
                            break
                except OSError:
                    pass
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass

            threading.Thread(target=watch, daemon=True).start()
            out, err = proc.communicate(stdin, timeout=timeout)
            resp = {"rc": proc.returncode, "wall_ms": int((time.monotonic() - t0) * 1000),
                    "stdout_b64": base64.b64encode(out or b"").decode(),
                    "stderr_b64": base64.b64encode(err or b"").decode()}
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.communicate(timeout=10)
            except Exception:
                pass
            resp = {"rc": -9, "wall_ms": int((time.monotonic() - t0) * 1000),
                    "stdout_b64": "", "stderr_b64": "", "error": "timeout"}
        except Exception as exc:  # report, never die
            resp = {"rc": -1, "wall_ms": int((time.monotonic() - t0) * 1000),
                    "stdout_b64": "", "stderr_b64": "", "error": f"{type(exc).__name__}: {exc}"}
        _send_response(conn, resp)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve(sock_path: str, ready_path: "str | None") -> None:
    os.umask(0o022)
    d = os.path.dirname(sock_path)
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        os.unlink(sock_path)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    os.chmod(sock_path, 0o600)
    srv.listen(64)
    if ready_path:
        with open(ready_path, "w") as fh:
            fh.write(str(os.getpid()))

    def stop(_signum, _frame):
        try:
            os.unlink(sock_path)
        except OSError:
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            continue
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    sock = None
    ready = None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--sock" and i + 1 < len(args):
            sock = args[i + 1]
        if a == "--ready" and i + 1 < len(args):
            ready = args[i + 1]
    if not sock:
        sys.stderr.write("usage: om_gpu_worker.py --sock PATH [--ready PATH]\n")
        raise SystemExit(2)
    serve(sock, ready)
