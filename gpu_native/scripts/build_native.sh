#!/bin/bash
# Compile OpenMontage's native (bionic) GPU components with Termux clang.
# Under proot, clang must run cc1 in-process (-fintegrated-cc1) and compile
# and link must be separate invocations, or it re-execs /proc/self/exe (proot).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OM_GPU_HOME="${OM_GPU_HOME:-/data/data/com.termux/files/home/.openmontage-gpu}"
T=/data/data/com.termux/files/usr
CC="$T/bin/clang -fintegrated-cc1"
LD="$T/bin/clang -fintegrated-cc1 --ld-path=$T/bin/ld.lld"
SRC="$REPO/gpu_native/src"
# Khronos EGL/GLES headers (from Termux mesa-dev + libglvnd-dev, header-only;
# binaries link the Android system EGL/GLES, i.e. the vendor Mali driver).
SDK_INC="$REPO/gpu_native/.runtime/sdk/x/data/data/com.termux/files/usr/include"
if [ ! -f "$SDK_INC/GLES3/gl3.h" ]; then
  python3 -I "$REPO/gpu_native/scripts/fetch_termux_debs.py" "$REPO/gpu_native/.runtime/debs" mesa-dev libglvnd-dev >/dev/null
  for d in "$REPO"/gpu_native/.runtime/debs/mesa-dev_*.deb "$REPO"/gpu_native/.runtime/debs/libglvnd-dev_*.deb; do
    dpkg-deb -x "$d" "$REPO/gpu_native/.runtime/sdk/x"
  done
fi
CC="$CC -I$SDK_INC"
OBJ="$OM_GPU_HOME/build"
mkdir -p "$OBJ" "$OM_GPU_HOME/bin" "$OM_GPU_HOME/prefix/lib"

# Binder thread-pool shim (linked as DT_NEEDED into codec executables).
$CC -O2 -fPIC -Wall -c -o "$OBJ/om_binder_pool.o" "$SRC/om_binder_pool.c"
$LD -shared -o "$OM_GPU_HOME/prefix/lib/libom_binder_pool.so" "$OBJ/om_binder_pool.o" -ldl

# Additional native tools are built by build_native.d/*.sh hooks.
for hook in "$REPO"/gpu_native/scripts/build_native.d/*.sh; do
  [ -f "$hook" ] || continue
  OM_GPU_HOME="$OM_GPU_HOME" CC="$CC" LD="$LD" SDK_INC="$SDK_INC" SRC="$SRC" OBJ="$OBJ" bash "$hook"
done
