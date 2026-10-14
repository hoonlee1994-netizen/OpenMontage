#!/bin/bash
# omgpu-probe: platform-attested hardware evidence (MediaCodec / Vulkan / EGL).
set -euo pipefail
$CC -O2 -Wall -Wno-unused-function -c -o "$OBJ/omgpu_probe.o" "$SRC/omgpu_probe.c"
$LD -o "$OM_GPU_HOME/bin/omgpu-probe" "$OBJ/omgpu_probe.o" -lmediandk -ldl
