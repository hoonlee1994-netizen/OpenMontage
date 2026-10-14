"""GOP / B-frame tuning for MediaCodec H.264 (quality per bit)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import NATIVE_FFMPEG, quality, run, save, table  # noqa: E402

SRC = Path(sys.argv[1])
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
results = []
for mbps, g, bf in [(5, 30, 0), (5, 60, 0), (5, 150, 0), (5, 150, 2), (5, 300, 0), (10, 150, 0), (10, 150, 2)]:
    o = OUT / f"h264_{mbps}M_g{g}_bf{bf}.mp4"
    cmd = [NATIVE_FFMPEG, "-y", "-v", "error", "-i", str(SRC), "-an", "-c:v", "h264_mediacodec",
           "-profile:v", "high", "-bitrate_mode", "vbr", "-b:v", f"{mbps}M", "-g", str(g), "-bf", str(bf),
           "-pix_fmt", "nv12", str(o)]
    r = run(f"hw h264 {mbps}M g{g} bf{bf}", cmd, output=o)
    if r.returncode == 0:
        r.quality = quality(o, SRC)
    results.append(r)
save(results, OUT / "gop.json")
print(table(results))
for r in results:
    if r.returncode:
        print(r.name, r.stderr_tail)
