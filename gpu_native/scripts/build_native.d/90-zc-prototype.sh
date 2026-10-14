#!/bin/bash
# Zero-copy transcode prototype (evaluated and rejected; kept so the
# measurement in gpu_native/README.md is reproducible). Not used in production.
set -euo pipefail
$CC -O2 -Wall -c -o "$OBJ/omgpu_zc_proto.o" "$SRC/omgpu_zc_proto.c"
$LD -o "$OM_GPU_HOME/bin/omgpu-zc-proto" "$OBJ/omgpu_zc_proto.o" -lmediandk \
  /system/lib64/libnativewindow.so /system/lib64/libEGL.so /system/lib64/libGLESv3.so -ldl -lm
# Drop Termux clang's default RUNPATH: Termux ships Mesa's libEGL.so, which
# would shadow the vendor (Mali) EGL driver.
PATCHELF="$(dirname "$0")/../../.runtime/tools-deb/x/usr/bin/patchelf"
"$PATCHELF" --remove-rpath "$OM_GPU_HOME/bin/omgpu-zc-proto"
