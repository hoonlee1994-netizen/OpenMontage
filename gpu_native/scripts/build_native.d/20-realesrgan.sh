#!/bin/bash
# Real-ESRGAN (ncnn, Vulkan compute) for GPU super-resolution.
#
# Vendors the locally built realesrgan-ncnn-vulkan (source:
# /root/.local/share/avc-vulkan/src/Real-ESRGAN-ncnn-vulkan, ncnn Vulkan
# backend) into the runtime with an $ORIGIN RUNPATH whose libvulkan.so.1 is the
# Android system loader, i.e. the vendor Mali driver with no ICD indirection.
# Only models that correspond to OpenMontage's approved PyTorch weights are
# installed: realesrgan-x4plus (RealESRGAN_x4plus) and realesrgan-x4plus-anime
# (RealESRGAN_x4plus_anime_6B).
set -euo pipefail
SRC_BIN="${OM_REALESRGAN_BIN:-/root/.local/share/avc-vulkan/build/realesrgan-ncnn-vulkan}"
SRC_MODELS="${OM_REALESRGAN_MODELS:-/data/data/com.termux/files/home/avc-vulkan/realesrgan/models}"
DST="$OM_GPU_HOME/realesrgan"
PATCHELF="$(dirname "$0")/../../.runtime/tools-deb/x/usr/bin/patchelf"
if [ ! -x "$SRC_BIN" ] || [ ! -d "$SRC_MODELS" ]; then
  echo "[realesrgan] source build not found; GPU super-resolution unavailable" >&2
  exit 0
fi
mkdir -p "$DST/bin" "$DST/lib" "$DST/models"
cp -f "$SRC_BIN" "$DST/bin/realesrgan-ncnn-vulkan"
"$PATCHELF" --set-rpath '$ORIGIN/../lib' "$DST/bin/realesrgan-ncnn-vulkan"
ln -sf /system/lib64/libvulkan.so "$DST/lib/libvulkan.so.1"
cp -f "$OM_GPU_HOME/prefix/lib/libc++_shared.so" "$DST/lib/libc++_shared.so"
for m in realesrgan-x4plus realesrgan-x4plus-anime; do
  cp -f "$SRC_MODELS/$m.param" "$SRC_MODELS/$m.bin" "$DST/models/"
done
( cd "$DST" && sha256sum bin/realesrgan-ncnn-vulkan models/* > SHA256SUMS )
