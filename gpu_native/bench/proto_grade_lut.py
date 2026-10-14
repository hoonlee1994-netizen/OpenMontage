"""Prototype: compile a pointwise FFmpeg grade chain to a 3D LUT and apply it
on CPU (lut3d) and on the Mali GPU (libplacebo), vs the original chain."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import NATIVE_FFMPEG, run, table  # noqa: E402

SRC = sys.argv[1]
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
CHAIN = ("colorbalance=rs=0.08:gs=0.02:bs=-0.05:rh=0.06:gh=0.02:bh=-0.04,"
         "curves=all='0/0.03 0.25/0.22 0.5/0.50 0.75/0.78 1/0.97',eq=contrast=1.05:saturation=1.1")
LEVEL = 8  # 64^3 HALD
hald = OUT / "hald_graded.png"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"haldclutsrc=level={LEVEL}",
                "-vf", CHAIN, "-frames:v", "1", "-pix_fmt", "rgb24", str(hald)], check=True)
img = cv2.cvtColor(cv2.imread(str(hald), cv2.IMREAD_UNCHANGED), cv2.COLOR_BGR2RGB).astype(np.float64) / 255.0
n = LEVEL * LEVEL
flat = img.reshape(-1, 3)  # HALD order: r fastest, then g, then b == .cube order
cube = OUT / "grade.cube"
with open(cube, "w") as fh:
    fh.write(f"LUT_3D_SIZE {n}\n")
    for r, g, b in flat:
        fh.write(f"{r:.6f} {g:.6f} {b:.6f}\n")
N = 300
common = ["-v", "error", "-t", str(N / 30)]
results = []
ref = OUT / "ref.nut"
results.append(run("CPU grade chain", [NATIVE_FFMPEG, *common, "-i", SRC, "-an", "-vf", CHAIN + ",format=yuv420p",
                   "-c:v", "rawvideo", "-y", str(ref)]))
lut_cpu = OUT / "lut_cpu.nut"
results.append(run("CPU lut3d (compiled)", [NATIVE_FFMPEG, *common, "-i", SRC, "-an",
                   "-vf", f"lut3d=file={cube}:interp=tetrahedral,format=yuv420p", "-c:v", "rawvideo", "-y", str(lut_cpu)]))
lut_gpu = OUT / "lut_gpu.nut"
results.append(run("GPU libplacebo LUT (Mali)", [NATIVE_FFMPEG, *common, "-init_hw_device", "vulkan=vk:0",
                   "-filter_hw_device", "vk", "-i", SRC, "-an", "-vf",
                   f"format=yuv420p,hwupload,libplacebo=lut={cube}:lut_type=normalized:dithering=-1:format=yuv420p,"
                   "hwdownload,format=yuv420p", "-c:v", "rawvideo", "-y", str(lut_gpu)]))
for r in results:
    r.frames = N
    r.fps = round(N / r.wall_s, 1)
    r.cpu_s_per_frame = round(r.cpu_s / N, 5)
print(table(results))
for r in results:
    if r.returncode:
        print(r.name, r.stderr_tail)
for name, f in (("CPU lut3d", lut_cpu), ("GPU libplacebo", lut_gpu)):
    p = subprocess.run([NATIVE_FFMPEG, "-v", "info", "-i", str(f), "-i", str(ref), "-lavfi", "[0][1]psnr;[0][1]ssim" if False else "psnr", "-f", "null", "-"],
                       capture_output=True, text=True)
    line = [l for l in p.stderr.splitlines() if "PSNR" in l]
    print(name, "vs CPU chain:", line[-1].split("]")[-1].strip() if line else p.stderr[-300:])
