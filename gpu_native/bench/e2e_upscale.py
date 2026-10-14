"""End-to-end: upscale tool, image path, CPU (PyTorch) vs GPU (ncnn/Vulkan)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from tools.enhancement.upscale import Upscale  # noqa: E402

src, out = sys.argv[1], sys.argv[2]
model = sys.argv[3] if len(sys.argv) > 3 else "RealESRGAN_x4plus"
scale = int(sys.argv[4]) if len(sys.argv) > 4 else 4
t0 = time.time()
r = Upscale().execute({"input_path": src, "output_path": out, "model": model, "scale": scale})
print(json.dumps({"mode": os.environ.get("OM_GPU_MODE"), "success": r.success, "wall_s": round(time.time() - t0, 2),
                  "error": r.error, "gpu_execution": (r.data or {}).get("gpu_execution"),
                  "size": [(r.data or {}).get("output_width"), (r.data or {}).get("output_height")]}, indent=1))
