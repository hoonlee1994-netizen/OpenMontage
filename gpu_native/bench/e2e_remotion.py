"""End-to-end: video_compose remotion_render through the real tool."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

props_file = Path(sys.argv[1])
out = Path(sys.argv[2])
props = json.loads(props_file.read_text())
props.setdefault("renderer_family", "explainer-data")

from tools.video.video_compose import VideoCompose  # noqa: E402

t0 = time.time()
req = {"operation": "remotion_render", "edit_decisions": props, "output_path": str(out)}
if os.environ.get("E2E_REMOTION_TIMEOUT_MS"):
    req["remotion_timeout_ms"] = int(os.environ["E2E_REMOTION_TIMEOUT_MS"])
res = VideoCompose().execute(req)
wall = time.time() - t0
print(json.dumps({
    "mode": os.environ.get("OM_GPU_MODE"), "success": res.success, "wall_s": round(wall, 2),
    "error": (res.error or "")[-800:], "gpu_execution": (res.data or {}).get("gpu_execution"),
}, indent=1))
