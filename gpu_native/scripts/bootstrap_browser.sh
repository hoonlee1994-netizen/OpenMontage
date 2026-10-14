#!/bin/bash
# Bootstrap the GPU Chromium runtime for Remotion (and HyperFrames).
#
# Installs Termux's Chromium 138 (bionic) and its dependency closure into
# $OM_GPU_HOME/browser. Its GPU process runs ANGLE on the Android EGL/GLES
# driver (Mali) - see gpu_native/README.md for why ANGLE's Vulkan backend is
# not used. Nothing is installed into the global Termux prefix.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OM_GPU_HOME="${OM_GPU_HOME:-/data/data/com.termux/files/home/.openmontage-gpu}"
TERMUX_USR=/data/data/com.termux/files/usr
CACHE="$REPO/gpu_native/.runtime"
DEBS="$CACHE/debs"
PATCHELF="$CACHE/tools-deb/x/usr/bin/patchelf"

# apt-cache depends --recurse chromium (x11 repo), minus python, fonts and
# software Vulkan ICDs (SwiftShader/lavapipe must never back the GPU path).
PKGS=(
  abseil-cpp adwaita-icon-theme adwaita-icon-theme-legacy at-spi2-core atk
  brotli chromium coreutils cups dbus desktop-file-utils fontconfig freetype
  fribidi gdbm gdk-pixbuf glib gtk-update-icon-cache gtk3 harfbuzz
  hicolor-icon-theme krb5 libandroid-execinfo libandroid-glob
  libandroid-posix-semaphore libandroid-selinux libandroid-shmem
  libandroid-support libbz2 libc++ libcairo libcrypt libdb libdrm libepoxy
  libevdev libevent libexpat libffi libflac libglvnd libgmp libgnutls
  libgraphite libiconv libicu libidn2 libjpeg-turbo libllvm libltdl liblzma
  liblzo libminizip libmp3lame libnettle libnghttp2 libngtcp2 libnspr libnss
  libogg libopus libpixman libpng libresolv-wrapper libsndfile libsoxr
  libsqlite libtiff libunbound libunistring libvorbis libwayland
  libwebrtc-audio-processing libx11 libxau libxcb libxcomposite libxcursor
  libxdamage libxdmcp libxext libxfixes libxft libxi libxinerama libxkbcommon
  libxml2 libxrandr libxrender libxshmfence libxtst libxxf86vm mesa ncurses
  ncurses-ui-libs opengl openssl pango pcre2 pulseaudio readline
  shared-mime-info speexdsp vulkan-loader vulkan-loader-android
  vulkan-loader-generic xkeyboard-config zlib zstd
)

log() { printf '[bootstrap-browser] %s\n' "$*"; }

fix_runpath() {
  local f="$1" rp="$2" cur
  cur="$("$PATCHELF" --print-rpath "$f" 2>/dev/null || true)"
  [ -n "$cur" ] || return 0
  [ "$cur" = "$rp" ] && return 0
  "$PATCHELF" --set-rpath "$rp" "$f"
}

[ -x "$PATCHELF" ] || { echo "run bootstrap_runtime.sh first (needs patchelf)" >&2; exit 1; }
mkdir -p "$OM_GPU_HOME" "$DEBS"
log "fetching ${#PKGS[@]} packages (sha256-verified)"
python3 -I "$REPO/gpu_native/scripts/fetch_termux_debs.py" "$DEBS" "${PKGS[@]}" >/dev/null

stage="$CACHE/stage-browser.$$"
mkdir -p "$stage"
for p in "${PKGS[@]}"; do
  deb="$(ls "$DEBS/${p}"_*.deb 2>/dev/null | head -1)"
  [ -n "$deb" ] || { echo "missing deb for $p" >&2; exit 1; }
  dpkg-deb -x "$deb" "$stage"
done
new="$OM_GPU_HOME/browser.new"
rm -rf "$new"
mkdir -p "$new"
cp -a "$stage$TERMUX_USR/." "$new/"
rm -rf "$stage"
rm -f "$new/lib/libmediandk.so" "$new/lib/libOpenSLES.so"

log "rewriting RUNPATHs to \$ORIGIN"
for f in "$new"/bin/* "$new"/libexec/* "$new"/lib/*.so* "$new"/lib/chromium/* "$new"/lib/*/*.so; do
  [ -f "$f" ] && [ ! -L "$f" ] && head -c4 "$f" 2>/dev/null | grep -q ELF || continue
  case "$f" in
    "$new"/lib/chromium/*|"$new"/lib/*/*.so) fix_runpath "$f" '$ORIGIN:$ORIGIN/..' ;;
    "$new"/lib/*) fix_runpath "$f" '$ORIGIN' ;;
    *) fix_runpath "$f" '$ORIGIN/../lib' ;;
  esac
done

# ANGLE's native-GLES backend loads libEGL.so.1 / libGLESv2.so.2: point them
# at the Android system (vendor Mali) drivers, not Termux's Mesa/glvnd copies.
C="$new/lib/chromium"
ln -sf /system/lib64/libEGL.so "$C/libEGL.so.1"
ln -sf /system/lib64/libGLESv2.so "$C/libGLESv2.so.2"
# Vulkan (if ever selected) goes straight to the Android loader.
[ -e "$C/libvulkan.so.1" ] && [ ! -L "$C/libvulkan.so.1" ] && mv "$C/libvulkan.so.1" "$C/libvulkan.so.1.khronos"
ln -sf /system/lib64/libvulkan.so "$C/libvulkan.so.1"

if [ -d "$OM_GPU_HOME/browser" ]; then
  mv "$OM_GPU_HOME/browser" "$OM_GPU_HOME/browser.old.$$"
fi
mv "$new" "$OM_GPU_HOME/browser"
rm -rf "$OM_GPU_HOME/browser.old.$$"
"$OM_GPU_HOME/browser/lib/chromium/headless_shell" --version
log "verify with: python -c 'from lib.gpu import remotion; print(remotion.verify_browser(refresh=True))'"
