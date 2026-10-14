"""Map libx264 CRF -> MediaCodec CQ quality by VMAF on real content."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import NATIVE_FFMPEG, quality, run, save, table  # noqa: E402

OUT = Path(sys.argv[1])
SOURCES = [Path(p) for p in sys.argv[2:]]
OUT.mkdir(parents=True, exist_ok=True)
results = []
for src in SOURCES:
    tag = src.stem[:12]
    for crf in (18, 23, 28):
        o = OUT / f"{tag}_x264_crf{crf}.mp4"
        r = run(f"{tag} x264 crf{crf} medium", ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-an", "-c:v", "libx264",
                "-crf", str(crf), "-preset", "medium", "-pix_fmt", "yuv420p", str(o)], output=o)
        r.quality = quality(o, src)
        results.append(r)
    for q in (55, 65, 72, 78, 84, 90, 95):
        o = OUT / f"{tag}_cq{q}.mp4"
        r = run(f"{tag} hw h264 cq{q}", [NATIVE_FFMPEG, "-y", "-v", "error", "-i", str(src), "-an", "-c:v", "h264_mediacodec",
                "-profile:v", "high", "-bitrate_mode", "cq", "-global_quality", str(q), "-g", "150",
                "-pix_fmt", "nv12", str(o)], output=o)
        if r.returncode == 0:
            r.quality = quality(o, src)
        results.append(r)
save(results, OUT / "cq.json")
print(table(results))
