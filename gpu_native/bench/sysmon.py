"""Whole-device CPU accounting for a workload, via per-process /proc/<pid>/stat.

usage: python3 sysmon.py <out.json> -- <cmd...>

Under proot /proc/stat is synthetic, so system-wide busy time is unavailable.
Instead every visible process is sampled every 0.25 s and the CPU time it
accumulated during the run is attributed by command name. This captures
Chromium, the Remotion compositor, codec helpers and our native runtime,
which RUSAGE_CHILDREN misses when processes are reparented or killed.
Processes that live shorter than one sample interval are missed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

HZ = os.sysconf("SC_CLK_TCK")


def snapshot() -> dict[int, tuple[str, int]]:
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as fh:
                s = fh.read()
        except OSError:
            continue
        r = s.rfind(")")
        comm = s[s.find("(") + 1:r]
        f = s[r + 2:].split()
        out[int(d)] = (comm, int(f[11]) + int(f[12]))  # utime + stime
    return out


def main() -> int:
    out = sys.argv[1]
    cmd = sys.argv[sys.argv.index("--") + 1:]
    base = snapshot()
    seen: dict[int, tuple[str, int, int]] = {}  # pid -> (comm, start_ticks, last_ticks)
    timeline: list[float] = []
    stop = threading.Event()

    def sample():
        prev_total = 0
        while not stop.wait(0.25):
            snap = snapshot()
            for pid, (comm, ticks) in snap.items():
                start = base[pid][1] if pid in base else 0
                if pid in seen:
                    seen[pid] = (comm, seen[pid][1], ticks)
                else:
                    seen[pid] = (comm, start, ticks)
            total = sum(v[2] - v[1] for v in seen.values())
            timeline.append(round((total - prev_total) / HZ / 0.25, 2))
            prev_total = total

    th = threading.Thread(target=sample, daemon=True)
    th.start()
    t0 = time.monotonic()
    rc = subprocess.call(cmd)
    wall = time.monotonic() - t0
    stop.set()
    th.join()
    by_comm: dict[str, float] = {}
    for comm, start, last in seen.values():
        by_comm[comm] = by_comm.get(comm, 0) + (last - start) / HZ
    by_comm = {k: round(v, 1) for k, v in sorted(by_comm.items(), key=lambda kv: -kv[1]) if v >= 0.1}
    total = round(sum(by_comm.values()), 1)
    res = {"cmd": cmd, "rc": rc, "wall_s": round(wall, 2), "cpu_core_s": total,
           "avg_busy_cores": round(total / wall, 2) if wall else None,
           "by_process": by_comm, "busy_cores_timeline_0.25s": timeline}
    with open(out, "w") as fh:
        json.dump(res, fh)
    print(json.dumps({k: v for k, v in res.items() if not k.startswith("busy_cores_timeline")}))
    return rc


if __name__ == "__main__":
    sys.exit(main())
