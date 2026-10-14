#!/bin/bash
# Bootstrap the OpenMontage GPU-native media runtime.
#
# Produces a self-contained, bionic (Android-linker) runtime that the Debian
# side drives as subprocesses:
#   * Termux FFmpeg 8.1 closure  -> MediaCodec hardware encode/decode,
#                                   Vulkan filters on the real Mali driver
#   * libom_binder_pool.so       -> binder thread pool for our own codec tools
#   * omgpu native tools         -> compiled from gpu_native/src
#
# Nothing is installed into the global Termux prefix or the Debian system.
#
# Location matters: the bionic linker selects its namespace configuration by
# the executable's path, and only /data and /tmp map to the [system] section
# that links the APEX media libraries. The proot rootfs (/root/...) is not
# visible as a /data path, so the runtime lives under Termux home.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OM_GPU_HOME="${OM_GPU_HOME:-/data/data/com.termux/files/home/.openmontage-gpu}"
TERMUX_USR=/data/data/com.termux/files/usr
CACHE="$REPO/gpu_native/.runtime"
DEBS="$CACHE/debs"
PATCHELF="$CACHE/tools-deb/x/usr/bin/patchelf"

# Termux FFmpeg dependency closure (apt-cache depends --recurse ffmpeg), minus
# python/fonts/software Vulkan ICDs. Pinned by the SHA256 in the apt-verified
# Packages index (see fetch_termux_debs.py).
PKGS=(
  brotli fftw fontconfig freetype fribidi game-music-emu giflib glib glslang
  harfbuzz libandroid-execinfo libandroid-glob libandroid-posix-semaphore
  libandroid-shmem libandroid-stub libandroid-support libaom libass libbluray
  libbs2b libbz2 libc++ libcairo libdav1d libdrm libexpat libffi libflac
  libgraphite libiconv libicu libjpeg-turbo liblzma liblzo libmp3lame
  libmpg123 libmysofa libogg libopencore-amr libopenmpt libopus libpixman
  libplacebo libpng librav1e libsamplerate libsndfile libsodium libsoxr libsrt
  libssh libtheora libtiff libudfread libv4l libvidstab libvmaf
  libvo-amrwbenc libvorbis libvpx libwayland libwebp libx11 libx264 libx265
  libxau libxcb libxdmcp libxext libxml2 libxrender libxshmfence libzimg
  libzmq littlecms ocl-icd pcre2 rubberband svt-av1 xvidcore zlib zstd ffmpeg
  openssl libsqlite libcrypt ncurses readline gdbm
)

log() { printf '[bootstrap] %s\n' "$*"; }

need_tools() {
  if [ ! -x "$PATCHELF" ]; then
    log "fetching patchelf (Debian .deb, extracted locally, not installed)"
    mkdir -p "$CACHE/tools-deb"
    (cd "$CACHE/tools-deb" && apt-get download patchelf >/dev/null)
    dpkg-deb -x "$(ls "$CACHE"/tools-deb/patchelf_*.deb | head -1)" "$CACHE/tools-deb/x"
  fi
}

fetch() {
  log "fetching ${#PKGS[@]} Termux packages (sha256-verified)"
  python3 -I "$REPO/gpu_native/scripts/fetch_termux_debs.py" "$DEBS" "${PKGS[@]}" >/dev/null
}

# Rewrite RUNPATH to an $ORIGIN-relative path. The new string is shorter than
# Termux's original "/data/data/com.termux/files/usr/lib", so patchelf
# overwrites it in place without relayouting the ELF (growing it corrupted
# PT_GNU_PROPERTY in libc++_shared.so with patchelf 0.14).
fix_runpath() {
  local f="$1" rp="$2" cur
  cur="$("$PATCHELF" --print-rpath "$f" 2>/dev/null || true)"
  [ -n "$cur" ] || return 0
  [ "$cur" = "$rp" ] && return 0
  "$PATCHELF" --set-rpath "$rp" "$f"
}

install_prefix() {
  local stage="$CACHE/stage.$$"
  mkdir -p "$stage"
  for p in "${PKGS[@]}"; do
    deb="$(ls "$DEBS/${p}"_*.deb 2>/dev/null | head -1)"
    [ -n "$deb" ] || { echo "missing deb for $p" >&2; exit 1; }
    dpkg-deb -x "$deb" "$stage"
  done
  local src="$stage$TERMUX_USR"
  # libandroid-stub ships stub libmediandk/libOpenSLES that shadow the real
  # system libraries and make every MediaCodec create call fail. Keep only the
  # libandroid.so stub: the real one drags in libicu, which the [system]
  # namespace does not link, and FFmpeg's buffer-mode codecs never call it.
  rm -f "$src/lib/libmediandk.so" "$src/lib/libOpenSLES.so"
  # Use the Android system Vulkan loader (direct path to the vendor Mali
  # driver) rather than the Khronos loader + ICD wrapper indirection.
  rm -f "$src"/lib/libvulkan.so*
  local new="$OM_GPU_HOME/prefix.new"
  rm -rf "$new"
  mkdir -p "$new"
  cp -a "$src/." "$new/"
  rm -rf "$stage"

  local f
  for f in "$new"/bin/*; do
    [ -f "$f" ] && [ ! -L "$f" ] && head -c4 "$f" | grep -q ELF && fix_runpath "$f" '$ORIGIN/../lib'
  done
  for f in "$new"/lib/*.so*; do
    [ -f "$f" ] && [ ! -L "$f" ] && head -c4 "$f" | grep -q ELF && fix_runpath "$f" '$ORIGIN'
  done
  for f in "$new"/libexec/*; do
    [ -f "$f" ] && [ ! -L "$f" ] && head -c4 "$f" | grep -q ELF && fix_runpath "$f" '$ORIGIN/../lib' || true
  done
  # Swap atomically; keep the previous prefix until the new one is in place.
  if [ -d "$OM_GPU_HOME/prefix" ]; then
    mv "$OM_GPU_HOME/prefix" "$OM_GPU_HOME/prefix.old.$$"
  fi
  mv "$new" "$OM_GPU_HOME/prefix"
  rm -rf "$OM_GPU_HOME/prefix.old.$$"
}

build_native() {
  log "building native components"
  "$REPO/gpu_native/scripts/build_native.sh"
  # Termux's FFmpeg main() already calls ABinderProcess_setThreadPoolMaxThreadCount(1)
  # + ABinderProcess_startThreadPool(), so it must NOT get libom_binder_pool.so
  # (a pre-started larger pool turns that call into an abort: "Binder
  # threadpool cannot be shrunk after starting"). The shim is for our own
  # native tools only.
}

main() {
  mkdir -p "$OM_GPU_HOME" "$DEBS"
  need_tools
  fetch
  install_prefix
  build_native
  python3 -I "$REPO/gpu_native/scripts/write_manifest.py" "$OM_GPU_HOME" "$DEBS" "${PKGS[@]}"
  log "runtime ready at $OM_GPU_HOME"
}

main "$@"
