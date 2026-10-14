"""Calibrate MediaCodec bitrate against libx264 CRF quality (VMAF)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import NATIVE_FFMPEG, quality, run, save, table  # noqa: E402

SRC = Path(sys.argv[1])
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
results = []

def cpu(crf: int, preset: str):
    o = OUT / f"x264_crf{crf}_{preset}.mp4"
    r = run(f"cpu libx264 crf{crf} {preset}", ["ffmpeg", "-y", "-v", "error", "-i", str(SRC), "-an",
            "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p", str(o)], output=o)
    r.quality = quality(o, SRC)
    results.append(r)

def hw(codec: str, mbps: float, profile: str | None = "high", mode: str = "vbr"):
    o = OUT / f"{codec}_{mbps}M_{profile}_{mode}.mp4"
    cmd = [NATIVE_FFMPEG, "-y", "-v", "error", "-i", str(SRC), "-an", "-c:v", f"{codec}_mediacodec",
           "-bitrate_mode", mode, "-b:v", f"{mbps}M", "-pix_fmt", "nv12"]
    if profile:
        cmd += ["-profile:v", profile]
    r = run(f"hw {codec} {mbps}M {profile} {mode}", cmd + [str(o)], output=o)
    if r.returncode == 0:
        r.quality = quality(o, SRC)
    results.append(r)

for crf, preset in [(23, "medium"), (18, "medium"), (23, "fast"), (18, "fast")]:
    cpu(crf, preset)
for mbps in (4, 6, 8, 12, 16):
    hw("h264", mbps)
hw("h264", 8, None)
for mbps in (3, 4, 6, 8, 12):
    hw("hevc", mbps, "main")
save(results, OUT / "calibration.json")
print(table(results))
for r in results:
    if r.returncode:
        print(r.name, r.stderr_tail)
