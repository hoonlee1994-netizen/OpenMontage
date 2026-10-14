#!/usr/bin/env python3
"""Profile a workload's CPU by process, with the PRoot tracer separated out.

usage: proot_profile.py <out.json> [--interval S] -- <cmd...>

Termux binaries (and their Android-linker children) all report ``comm ==
"linker64"``, so /proc/<pid>/stat's comm is useless for attribution here.
This profiler reads /proc/<pid>/cmdline instead and keeps the PRoot tracer
(TracerPid of this process) as its own bucket.

Emits: wall, per-bucket CPU, the busy-cores timeline (to see whether the extra
tracer CPU can hide in idle cores), and the tracer's net CPU after subtracting
its ambient rate measured in an idle window on either side.
"""
from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import time

HZ = os.sysconf("SC_CLK_TCK")


def proc_info(pid: int):
    """(label, ticks) for a pid, or None.  label comes from cmdline."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            s = fh.read()
    except OSError:
        return None
    r = s.rfind(")")
    f = s[r + 2:].split()
    try:
        ticks = int(f[11]) + int(f[12])
    except (IndexError, ValueError):
        return None
    label = None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
        parts = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
        if parts:
            exe = os.path.basename(parts[0])
            extra = ""
            for p in parts[1:]:
                if p.startswith("--type="):
                    extra = ":" + p.split("=", 1)[1]
                    break
                if p.startswith("--utility-sub-type="):
                    extra = ":" + p.split("=", 1)[1].split(",")[0]
                    break
            label = exe + extra
    except OSError:
        pass
    if not label:
        label = "?"
    return label, ticks


def snapshot():
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        info = proc_info(int(d))
        if info:
            out[int(d)] = info
    return out


def tracer_pid() -> int:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("TracerPid:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def idle_rate(tracer: int, seconds: float) -> float:
    def t():
        info = proc_info(tracer)
        return info[1] if info else None
    a = t()
    time.sleep(seconds)
    b = t()
    if a is None or b is None:
        return 0.0
    return max(0.0, (b - a) / HZ) / seconds


def main() -> int:
    out_path = sys.argv[1]
    argv = sys.argv[2:]
    interval = 0.25
    idle_s = 5.0
    for flag, setter in (("--interval", "i"), ("--idle", "d")):
        if flag in argv:
            i = argv.index(flag)
            if flag == "--interval":
                interval = float(argv[i + 1])
            else:
                idle_s = float(argv[i + 1])
            argv = argv[:i] + argv[i + 2:]
    cmd = argv[argv.index("--") + 1:]

    tracer = tracer_pid()
    rate_before = idle_rate(tracer, idle_s) if tracer else 0.0

    base = snapshot()
    seen: dict[int, list] = {}          # pid -> [label, start, last]
    timeline: list[float] = []
    t_a = proc_info(tracer)[1] if tracer else None

    import threading
    stop = threading.Event()

    def sample():
        prev = 0.0
        while not stop.wait(interval):
            snap = snapshot()
            for pid, (label, ticks) in snap.items():
                if pid in seen:
                    seen[pid][2] = ticks
                else:
                    start = base[pid][1] if pid in base else 0
                    seen[pid] = [label, start, ticks]
            total = sum(v[2] - v[1] for v in seen.values())
            timeline.append(round((total - prev) / HZ / interval, 2))
            prev = total

    th = threading.Thread(target=sample, daemon=True)
    th.start()
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    wall = time.monotonic() - t0
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    stop.set()
    th.join()
    t_b = proc_info(tracer)[1] if tracer else None
    rate_after = idle_rate(tracer, idle_s) if tracer else 0.0

    by_label: dict[str, float] = {}
    tracer_seen = 0.0
    for pid, (label, start, last) in seen.items():
        cpu = (last - start) / HZ
        if pid == tracer:
            tracer_seen += cpu
            continue
        if cpu >= 0.05:
            by_label[label] = by_label.get(label, 0.0) + cpu
    by_label = {k: round(v, 1) for k, v in sorted(by_label.items(), key=lambda kv: -kv[1])}

    children_cpu = ((r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime))
    tracer_cpu = ((t_b - t_a) / HZ) if (t_a is not None and t_b is not None) else tracer_seen
    ambient_rate = min(rate_before, rate_after)
    tracer_net = max(0.0, tracer_cpu - ambient_rate * wall)
    total_cpu = sum(by_label.values()) + tracer_net

    res = {
        "cmd": cmd,
        "rc": proc.returncode,
        "tracer_pid": tracer,
        "wall_s": round(wall, 3),
        "workload_cpu_s_rusage": round(children_cpu, 2),
        "total_cpu_s": round(total_cpu, 2),
        "avg_cores": round(total_cpu / wall, 2) if wall else None,
        "peak_cores": max(timeline) if timeline else None,
        "cores_timeline": timeline,
        "tracer_cpu_s": round(tracer_net, 3),
        "tracer_ambient_rate": round(ambient_rate, 3),
        "tracer_pct_of_total_cpu": round(100.0 * tracer_net / total_cpu, 1) if total_cpu else None,
        "tracer_pct_of_wall_x8": round(100.0 * tracer_net / (wall * 8), 1) if wall else None,
        "by_process": by_label,
        "stdout_tail": proc.stdout[-500:],
        "stderr_tail": proc.stderr[-300:],
    }
    with open(out_path, "w") as fh:
        json.dump(res, fh, indent=1)
    print(json.dumps({k: v for k, v in res.items()
                      if k not in ("cores_timeline", "stdout_tail", "stderr_tail")}, indent=1))
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
