"""GPU-native execution layer for OpenMontage.

``activate()`` is called when the ``tools`` package is imported. It makes
GPU-first execution the default for the whole process tree by putting the
dispatcher shims (``gpu_native/shims/path``) first on ``PATH``: every
``ffmpeg`` launched by a tool, by Remotion's encoder or by HyperFrames then
goes through :mod:`lib.gpu.ffmpeg_exec`, which runs video encodes on the
verified hardware encoder and falls back to the Debian CPU build on failure.

Nothing here touches system configuration; the effect is limited to the
current process and its children. ``OM_GPU_MODE=cpu`` disables it.
"""
from __future__ import annotations

import os

_ACTIVATED = False


def activate() -> bool:
    """Install the GPU-first dispatch shims for this process (idempotent)."""
    global _ACTIVATED
    if _ACTIVATED:
        return True
    import shutil

    from lib.gpu import runtime

    mode = runtime.mode()
    # Resolve once and pin for children: the shim must not re-load config.
    os.environ["OM_GPU_MODE"] = mode
    if mode == "cpu" or not runtime.runtime_installed():
        return False
    shim = str(runtime.shim_dir())
    path = os.environ.get("PATH", "")
    if shim not in path.split(os.pathsep):
        real = shutil.which("ffmpeg", path=path)
        if real:
            os.environ.setdefault("OM_GPU_REAL_FFMPEG", real)
        os.environ["PATH"] = shim + (os.pathsep + path if path else "")
    _ACTIVATED = True
    return True


__all__ = ["activate"]
