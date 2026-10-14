"""Collect hardware-execution evidence for the GPU-native paths.

For each backend this captures evidence *while the real workload runs*:

* MediaCodec encode: the platform codec info (omgpu-probe) and FFmpeg's own
  runtime log naming the component it instantiated.
* Chromium/Remotion: the render's GPU process command line, the vendor Mali
  GLES driver mapped in it, and its open /dev/mali0 descriptor.
* ncnn super-resolution: the vendor Vulkan driver mapped and /dev/mali0 open
  in the inference process, plus ncnn's device line.
* Debian/glibc contrast: what the Debian side's own Vulkan resolves to.

usage: python3 collect_evidence.py <out_dir>
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from lib.gpu import runtime  # noqa: E402

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
SRC = str(REPO / "assets" / "signal-from-tomorrow-demo.mp4")
evidence: dict = {"collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def proc_snapshot(pid: str) -> dict:
    snap: dict = {"pid": pid}
    try:
        snap["cmdline"] = [a for a in open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "replace").split("\0")
                           if a.startswith(("--type", "--use-gl", "--use-angle")) or "/" in a][:6]
        maps = open(f"/proc/{pid}/maps").read().splitlines()
        snap["vendor_gpu_libs_mapped"] = sorted({l.split()[-1] for l in maps
                                                 if any(k in l for k in ("mali", "GLES_mali", "vulkan."))})
        fds = []
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                tgt = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if "mali" in tgt:
                fds.append(tgt)
        snap["dev_mali0_open"] = "/dev/mali0" in fds
    except OSError as exc:
        snap["error"] = str(exc)
    return snap


def watch(match, stop: threading.Event, found: dict):
    """Snapshot matching processes repeatedly while they run; keep the
    richest snapshot per pid (drivers are mapped / devices opened lazily)."""
    while not stop.is_set():
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                argv = open(f"/proc/{d}/cmdline", "rb").read().split(b"\0")
            except OSError:
                continue
            if not match(argv):
                continue
            snap = proc_snapshot(d)
            prev = found.get(d)
            score = (snap.get("dev_mali0_open", False), len(snap.get("vendor_gpu_libs_mapped", [])))
            if prev is None or score > (prev.get("dev_mali0_open", False), len(prev.get("vendor_gpu_libs_mapped", []))):
                found[d] = snap
        stop.wait(1.0)


# 1. Platform-attested capabilities.
probe = subprocess.run([str(runtime.probe_binary())], capture_output=True, text=True, env=runtime.native_env())
evidence["omgpu_probe"] = json.loads(probe.stdout)

# 2. MediaCodec encode: FFmpeg names the component it instantiated.
enc_out = OUT / "evidence_encode.mp4"
p = subprocess.run([str(runtime.native_ffmpeg()), "-y", "-v", "debug", "-t", "3", "-i", SRC, "-an",
                    "-c:v", "h264_mediacodec", "-bitrate_mode", "cq", "-global_quality", "90", str(enc_out)],
                   capture_output=True, text=True, env=runtime.native_env())
evidence["mediacodec_encoder_runtime_log"] = sorted({l.split("]")[-1].split(" output format")[0].strip()
                                                     for l in p.stderr.splitlines() if "MediaCodec encoder" in l})

# 3. Chromium GPU process during a real Remotion render.
stop = threading.Event()
found: dict = {}
# Zygote-forked: argv[0] is not headless_shell, so key on the GPU-process flags.
t = threading.Thread(target=watch, args=(lambda argv: b"--type=gpu-process" in argv and b"--use-angle=gles-egl" in argv,
                                         stop, found))
t.start()
props = REPO / "remotion-composer" / "public" / "demo-props" / "world-in-numbers.json"
rr = subprocess.run([str(REPO / ".venv" / "bin" / "python"), str(REPO / "gpu_native" / "bench" / "e2e_remotion.py"),
                     str(props), str(OUT / "evidence_remotion.mp4")], capture_output=True, text=True,
                    env=dict(os.environ, OM_GPU_MODE="require"))
stop.set()
t.join()
evidence["remotion_render_require_mode"] = rr.stdout[-1200:]
evidence["chromium_gpu_processes"] = list(found.values())

# 4. ncnn super-resolution process.
stop = threading.Event()
found = {}
t = threading.Thread(target=watch, args=(lambda argv: argv[0].endswith(b"realesrgan-ncnn-vulkan"), stop, found))
t.start()
img = OUT / "evidence_sr_in.png"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "5", "-i", SRC, "-frames:v", "1", "-vf", "scale=640:360", str(img)],
               check=True)
from lib.gpu import superres  # noqa: E402

info = superres.run(img, OUT / "evidence_sr_out.png", "RealESRGAN_x4plus")
stop.set()
t.join()
evidence["superres_run"] = info
evidence["superres_processes"] = list(found.values())

# 5. Debian/glibc contrast: the Debian Vulkan loader only finds software devices.
try:
    import ctypes

    vk = ctypes.CDLL("libvulkan.so.1")
    evidence["debian_glibc_vulkan"] = "loaded " + str(vk) + " (see prior audit: llvmpipe only)"
except OSError as exc:
    evidence["debian_glibc_vulkan"] = f"not loadable: {exc}"
dpkg = subprocess.run(["sh", "-c", "ls /usr/share/vulkan/icd.d/ 2>/dev/null"], capture_output=True, text=True)
evidence["debian_vulkan_icds"] = dpkg.stdout.split()

(OUT / "hardware_evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
print(json.dumps({k: v for k, v in evidence.items() if k not in ("omgpu_probe",)}, indent=2)[:6000])
