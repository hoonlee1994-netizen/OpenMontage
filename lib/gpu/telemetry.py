"""Append-only execution log for GPU-native dispatch decisions.

Every routed media job records which backend actually executed it (hardware
encoder component, GPU device, or CPU) and why. This is the evidence trail
that keeps GPU claims honest: a job that silently fell back to CPU shows up
here as ``backend: "cpu"`` with the fallback reason.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

from lib.gpu import runtime

_MAX_BYTES = 8 * 1024 * 1024


def log_path() -> Path:
    return Path(os.environ.get("OM_GPU_EXEC_LOG") or (runtime.state_dir() / "exec.jsonl"))


def record(event: dict[str, Any]) -> None:
    event = {"t": round(time.time(), 3), "pid": os.getpid(), **event}
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                if fh.tell() > _MAX_BYTES:
                    # Rotate rather than grow without bound in unattended runs.
                    rotated = path.with_suffix(".1.jsonl")
                    os.replace(path, rotated)
                    with open(path, "a", encoding="utf-8") as fresh:
                        fresh.write(json.dumps(event) + "\n")
                    return
                fh.write(json.dumps(event) + "\n")
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        pass  # telemetry must never break production


def since(t0: float) -> list[dict[str, Any]]:
    """Events recorded at or after ``t0`` (epoch seconds)."""
    out = []
    try:
        with open(log_path(), encoding="utf-8") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("t", 0) >= t0:
                    out.append(ev)
    except OSError:
        pass
    return out


def summarize(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Compact per-backend summary for attaching to a ToolResult."""
    jobs = []
    counts: dict[str, int] = {}
    for ev in events:
        if ev.get("kind") != "job":
            continue
        backend = ev.get("backend", "?")
        counts[backend] = counts.get(backend, 0) + 1
        jobs.append({
            k: ev.get(k) for k in
            ("op", "backend", "device", "encoder", "fallback_reason", "wall_s", "output")
            if ev.get(k) is not None
        })
    return {"by_backend": counts, "jobs": jobs[-50:]}


def for_outputs(t0: float, paths) -> dict[str, Any]:
    """Summary of jobs since ``t0`` that wrote any of ``paths``.

    A path that is a directory matches every job writing inside it (e.g. a
    compose temp dir), so concurrent jobs in one process are not mixed up.
    """
    import os

    norm = [os.path.abspath(str(p)) for p in paths if p]
    picked = []
    for ev in since(t0):
        outs = [os.path.abspath(o) for o in str(ev.get("output") or "").split(",") if o]
        if any(o == p or o.startswith(p.rstrip("/") + "/") for o in outs for p in norm):
            picked.append(ev)
    summary = summarize(picked)
    backends = summary["by_backend"]
    summary["backend"] = ("gpu" if backends and set(backends) == {"gpu"} else
                          "cpu" if backends and "gpu" not in backends else
                          "mixed" if backends else "none")
    return summary
