"""Decode throughput: software (CPU) vs MediaCodec (hardware) decoders."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import NATIVE_FFMPEG, run, save, table  # noqa: E402

SRC = Path(sys.argv[1])
OUT = Path(sys.argv[2])
codec = sys.argv[3] if len(sys.argv) > 3 else "h264"
results = []
for i in range(2):
    results.append(run(f"debian sw decode {codec} #{i}", ["ffmpeg", "-v", "error", "-i", str(SRC), "-an", "-f", "null", "-"]))
    results.append(run(f"native sw decode {codec} #{i}", [NATIVE_FFMPEG, "-v", "error", "-i", str(SRC), "-an", "-f", "null", "-"]))
    results.append(run(f"native mediacodec decode {codec} #{i}", [NATIVE_FFMPEG, "-v", "error", "-c:v", f"{codec}_mediacodec", "-i", str(SRC), "-an", "-f", "null", "-"]))
for r in results:
    r.frames = 900 if "signal" in SRC.name else None
    if r.frames:
        r.fps = round(r.frames / r.wall_s, 1)
        r.cpu_s_per_frame = round(r.cpu_s / r.frames, 5)
save(results, OUT / f"decode_{codec}.json")
print(table(results))
for r in results:
    if r.returncode:
        print(r.name, r.stderr_tail)
