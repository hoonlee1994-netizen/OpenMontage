"""CPU filter cost vs Vulkan (libplacebo on Mali) incl. upload/download."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import NATIVE_FFMPEG, run, save, table  # noqa: E402

SRC = sys.argv[1]
OUT = Path(sys.argv[2])
GRADE = ("colorbalance=rs=0.08:gs=0.02:bs=-0.05:rh=0.06:gh=0.02:bh=-0.04,"
         "curves=all='0/0.03 0.25/0.22 0.5/0.50 0.75/0.78 1/0.97',eq=contrast=1.05:saturation=1.1")
VK = ["-init_hw_device", "vulkan=vk:0", "-filter_hw_device", "vk"]
cases = [
    ("decode only", []),
    ("decode + grade chain (CPU)", ["-vf", GRADE]),
    ("decode + upload/download (Vulkan)", VK + ["-vf", "format=yuv420p,hwupload,hwdownload,format=yuv420p"]),
    ("decode + libplacebo pass (Mali)", VK + ["-vf", "format=yuv420p,hwupload,libplacebo=w=1920:h=1080:format=yuv420p,hwdownload,format=yuv420p"]),
    ("decode + scale lanczos 4K (CPU)", ["-vf", "scale=3840:2160:flags=lanczos"]),
    ("decode + libplacebo 4K ewa (Mali)", VK + ["-vf", "format=yuv420p,hwupload,libplacebo=w=3840:h=2160:upscaler=ewa_lanczos:format=yuv420p,hwdownload,format=yuv420p"]),
    ("decode + gblur s=8 (CPU)", ["-vf", "gblur=sigma=8"]),
    ("decode + gblur_vulkan s=8 (Mali)", VK + ["-vf", "format=yuv420p,hwupload,gblur_vulkan=sigma=8,hwdownload,format=yuv420p"]),
]
results = []
for name, extra in cases:
    pre = [a for a in extra if a in ("-init_hw_device", "vulkan=vk:0", "-filter_hw_device", "vk")]
    post = [a for a in extra if a not in pre]
    r = run(name, [NATIVE_FFMPEG, "-v", "error", *pre, "-i", SRC, "-an", *post, "-f", "null", "-"])
    r.frames = 900
    r.fps = round(900 / r.wall_s, 1)
    r.cpu_s_per_frame = round(r.cpu_s / 900, 5)
    results.append(r)
save(results, OUT / "filters.json")
print(table(results))
for r in results:
    if r.returncode:
        print(r.name, r.stderr_tail)
