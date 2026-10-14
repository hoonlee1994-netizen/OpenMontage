"""Sustained batch: N tool-style transcodes, P at a time, through the PATH ffmpeg.

Each job is the same CPU-oriented command OpenMontage tools build
(libx264 -crf 23 -preset medium); OM_GPU_MODE decides the backend.
Prints aggregate frames/sec over the whole batch.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SHIM = str(REPO / "gpu_native" / "shims" / "path" / "ffmpeg")
SRC = str(REPO / "assets" / "signal-from-tomorrow-demo.mp4")
out_dir = Path(sys.argv[1])
n, par = int(sys.argv[2]), int(sys.argv[3])
out_dir.mkdir(parents=True, exist_ok=True)


def job(i: int):
    out = out_dir / f"job{i}.mp4"
    t0 = time.time()
    rc = subprocess.run([SHIM, "-y", "-v", "error", "-i", SRC, "-vf", "scale=1920:1080,setsar=1,fps=30",
                         "-c:v", "libx264", "-crf", "23", "-preset", "medium", "-pix_fmt", "yuv420p",
                         "-c:a", "aac", "-b:a", "192k", str(out)]).returncode
    p = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(out)],
                       capture_output=True, text=True)
    return {"job": i, "rc": rc, "wall_s": round(time.time() - t0, 2),
            "frames": int(p.stdout.strip().rstrip(",") or 0) if rc == 0 else 0}


t0 = time.time()
with ThreadPoolExecutor(max_workers=par) as pool:
    res = list(pool.map(job, range(n)))
wall = time.time() - t0
total = sum(r["frames"] for r in res)
print(json.dumps({"jobs": res, "wall_s": round(wall, 2), "total_frames": total,
                  "aggregate_fps": round(total / wall, 1), "failures": sum(r["rc"] != 0 for r in res)}))
