"""End-to-end: video_compose 'compose' (FFmpeg path) through the real tool.

Workload: 6 cuts from two real sources (1080p30 production render and a
416x752 24fps AI clip), normalised to 1920x1080@30 (letterbox), concatenated,
subtitles burned with libass. This is the talking-head / clip-factory path.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

out = Path(sys.argv[1])
A = str(REPO / "assets" / "signal-from-tomorrow-demo.mp4")
B = "/sdcard/Download/grok_video_2026-09-22-00-22-19.mp4"
cuts = [
    {"id": "c1", "source": A, "in_seconds": 0.0, "out_seconds": 6.0},
    {"id": "c2", "source": B, "in_seconds": 1.0, "out_seconds": 6.0},
    {"id": "c3", "source": A, "in_seconds": 8.5, "out_seconds": 14.0},
    {"id": "c4", "source": A, "in_seconds": 20.0, "out_seconds": 26.0, "speed": 1.25},
    {"id": "c5", "source": B, "in_seconds": 4.0, "out_seconds": 9.5},
    {"id": "c6", "source": A, "in_seconds": 26.0, "out_seconds": 30.0},
]
srt = out.with_suffix(".srt")
lines = []
t = 0.0
for i in range(14):
    s, e = t, t + 2.2
    fmt = lambda x: f"{int(x//3600):02d}:{int(x%3600//60):02d}:{int(x%60):02d},{int(round((x%1)*1000)):03d}"
    lines += [str(i + 1), f"{fmt(s)} --> {fmt(e)}", f"Subtitle line {i + 1}: GPU-native production test", ""]
    t += 2.4
srt.write_text("\n".join(lines), encoding="utf-8")

from tools.video.video_compose import VideoCompose  # noqa: E402

t0 = time.time()
res = VideoCompose().execute({
    "operation": "compose", "edit_decisions": {"cuts": cuts}, "output_path": str(out),
    "subtitle_path": str(srt),
})
print(json.dumps({"mode": os.environ.get("OM_GPU_MODE"), "success": res.success,
                  "wall_s": round(time.time() - t0, 2), "error": (res.error or "")[-600:]}, indent=1))
