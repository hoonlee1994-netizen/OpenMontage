"""GPU super-resolution backend: Real-ESRGAN on ncnn/Vulkan (Mali).

Same networks and weights as OpenMontage's PyTorch path, converted to ncnn:
``RealESRGAN_x4plus`` -> ``realesrgan-x4plus`` and
``RealESRGAN_x4plus_anime_6B`` -> ``realesrgan-x4plus-anime``. Inference runs
as Vulkan compute shaders on the Mali GPU through the Android system Vulkan
loader. Models without an ncnn equivalent (``RealESRNet_x4plus``), GFPGAN face
enhancement, alpha and 16-bit inputs stay on the PyTorch path; the caller
decides via :func:`gpu_support`.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from lib.gpu import runtime, telemetry

NCNN_MODELS = {
    "RealESRGAN_x4plus": "realesrgan-x4plus",
    "RealESRGAN_x4plus_anime_6B": "realesrgan-x4plus-anime",
}
BACKEND = "ncnn-vulkan-superres"


def _root() -> Path:
    return runtime.home() / "realesrgan"


def binary() -> Path:
    return _root() / "bin" / "realesrgan-ncnn-vulkan"


def available(model_name: str) -> bool:
    ncnn = NCNN_MODELS.get(model_name)
    if not ncnn or not binary().is_file():
        return False
    return (_root() / "models" / f"{ncnn}.param").is_file()


def gpu_support(model_name: str, *, face_enhance: bool, image=None) -> tuple[bool, str]:
    """Can this request run on the GPU backend with identical semantics?"""
    if runtime.mode() == "cpu":
        return False, "OM_GPU_MODE=cpu"
    if face_enhance:
        return False, "GFPGAN face enhancement has no GPU backend (PyTorch CPU)"
    if model_name not in NCNN_MODELS:
        return False, f"{model_name} has no ncnn/Vulkan conversion"
    if not available(model_name):
        return False, "ncnn Real-ESRGAN runtime not installed"
    caps = runtime.capabilities()
    if not (caps.get("ok") and (caps.get("vulkan") or {}).get("verified")):
        return False, "Mali Vulkan device not verified"
    if not runtime.healthy(BACKEND):
        return False, "GPU super-resolution backend unhealthy (recent failures)"
    if image is not None:
        if image.dtype.itemsize != 1:
            return False, "16-bit image (ncnn path is 8-bit)"
        if image.ndim == 3 and image.shape[2] == 4:
            return False, "alpha channel (PyTorch path upsamples alpha with the model)"
    return True, "ok"


def run(input_path: Path, output_dir_or_file: Path, model_name: str, *,
        tile: int = 0, threads: str = "1:2:2", timeout: Optional[int] = None) -> dict[str, Any]:
    """Run x4 inference on a file or a directory of PNG frames."""
    ncnn = NCNN_MODELS[model_name]
    cmd = [str(binary()), "-i", str(input_path), "-o", str(output_dir_or_file),
           "-n", ncnn, "-s", "4", "-t", str(tile), "-m", str(_root() / "models"),
           "-j", threads, "-f", "png"]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                          timeout=timeout, env=runtime.native_env())
    wall = round(time.time() - t0, 3)
    device = None
    for line in (proc.stderr or "").splitlines():
        if line.startswith("[0 ") and "queueC" in line:
            device = line[3:line.index("]")]
    ok = proc.returncode == 0 and Path(output_dir_or_file).exists() and device is not None
    runtime.record_outcome(BACKEND, ok, (proc.stderr or "")[-300:])
    telemetry.record({"kind": "job", "op": "superres", "backend": "gpu" if ok else "gpu-failed",
                      "device": device, "model": ncnn, "wall_s": wall, "output": str(output_dir_or_file)})
    if not ok:
        raise RuntimeError(f"ncnn Real-ESRGAN failed (exit {proc.returncode}, device={device}): "
                           f"{(proc.stderr or '').strip()[-400:]}")
    return {"backend": "gpu", "device": device, "engine": "ncnn-vulkan", "model": ncnn, "wall_s": wall}


def threads_for_frames() -> str:
    # Overlap PNG decode/encode (CPU) with inference (GPU) for frame batches.
    return os.environ.get("OM_SUPERRES_THREADS", "2:2:2")
