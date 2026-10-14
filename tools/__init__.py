"""OpenMontage tools.

Importing this package activates GPU-first media execution for the process
(see ``lib/gpu``): video encodes run on the hardware encoder and Remotion
renders on the GPU by default, with automatic CPU fallback.
``OM_GPU_MODE=cpu`` (or ``gpu.mode: cpu`` in config.yaml) disables it.
"""

try:
    from lib.gpu import activate as _activate_gpu

    _activate_gpu()
except Exception:  # GPU activation must never prevent the tools from loading
    pass
