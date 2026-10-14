# OpenMontage GPU-native runtime

OpenMontage runs inside a PRoot Debian (glibc) userland on Android. glibc
processes there can only reach *software* Vulkan/GL (llvmpipe, lavapipe,
SwiftShader). The genuine hardware — the **Mali-G720 MC8 GPU** (Vulkan 1.3,
OpenGL ES 3.2) and the **MediaTek hardware video codecs** (Codec2 /
MediaCodec) — is reachable only from **bionic** (Android-linker) processes.

This directory builds and drives a project-scoped bionic runtime so that
GPU-compatible work runs on that hardware **by default**, with automatic,
logged CPU fallback.

## What runs where (default, `OM_GPU_MODE=auto`)

| Workload | Backend | Evidence of genuine hardware |
|---|---|---|
| H.264/HEVC encode — every tool's FFmpeg encode, Remotion's frame encoder, HyperFrames | MediaTek hardware encoder `c2.mtk.avc.encoder` / `c2.mtk.hevc.encoder` | `AMediaCodecInfo_getMediaCodecInfoType() == HARDWARE_ACCELERATED`, vendor codec |
| Remotion frame rasterisation/compositing | Mali GPU via bionic Chromium 138 + ANGLE (`gles-egl`) on the Android GLES driver | WebGL renderer `ANGLE (ARM, Mali-G720 MC8, OpenGL ES 3.2)`, per-render GPU-process command line |
| Super-resolution (`RealESRGAN_x4plus`, `RealESRGAN_x4plus_anime_6B`) | Real-ESRGAN on ncnn, Vulkan compute on Mali | `[0 Mali-G720 MC8]` device, `/dev/mali0`, `vulkan.mali.so` mapped |
| Independent compose segments | Scheduled concurrently (hardware encoder sessions) | — |
| Video decode, FFmpeg filters (scale/pad/libass/colour grade), audio | CPU — measured faster or required for exact semantics (see below) | — |

`python -c "from lib.gpu import runtime; import json; print(json.dumps(runtime.summary(), indent=2))"`
prints the verified hardware and this routing table; the same block appears in
preflight (`registry.provider_menu_summary()["gpu_runtime"]`).

## Architecture

```
Debian / glibc (OpenMontage Python, Node, Remotion)
 ├─ lib/gpu/runtime.py      probe + verify hardware, policy, health breaker, codec slots
 ├─ lib/gpu/ffmpeg_exec.py  PATH "ffmpeg" dispatcher (gpu_native/shims/path/ffmpeg)
 │    └─ lib/gpu/ffmpeg_args.py  translate libx264/libx265 -> h264/hevc_mediacodec
 ├─ lib/gpu/remotion.py     GPU Chromium + hardware-encoder hook for Remotion renders
 ├─ lib/gpu/superres.py     ncnn/Vulkan Real-ESRGAN backend for the upscale tool
 └─ lib/gpu/oop.py          client for the PRoot-free worker (file-output encodes)
          │  exec (bionic processes, still under proot)
          │        └─ app-spawn (am startservice) ─▶ worker/om_gpu_worker.py
          │                                             (bionic, TracerPid 0)
          ▼
$OM_GPU_HOME (/data/data/com.termux/files/home/.openmontage-gpu)
 ├─ prefix/      Termux FFmpeg 8.1 closure (MediaCodec + Vulkan), $ORIGIN RUNPATHs
 ├─ browser/     Termux Chromium 138 headless_shell closure (ANGLE)
 ├─ realesrgan/  ncnn Real-ESRGAN + approved models
 ├─ bin/         omgpu-probe (+ prototypes)
 ├─ run/         worker.sock / worker.ready (PRoot-free worker)
 └─ state/       capabilities.json, chromium_gpu.json, health.json, exec.jsonl
```

### PRoot-free execution (bounded, measured)

Every process inside PRoot is traced, so each native FFmpeg launch pays for the
tracer's path translation of the bionic runtime's dynamic linking (83
libraries) — measured **1.42 s vs 0.335 s** for `ffmpeg -version`. A traced
process cannot detach itself: PRoot 5.1's seccomp filter (`Seccomp: 2`)
answers `ENOSYS` to the translated syscalls once no tracer is attached, and
seccomp filters only stack. The only PRoot-free source is a process the Termux
*app* forks, so `lib/gpu/oop.py` starts the worker with
`am startservice com.termux.service.action.service_execute` — a service intent,
**not** `RUN_COMMAND`, so it needs no `allow-external-apps`, no permission and
no Android setting change. The worker then runs native commands over a Unix
socket; any failure falls back to the in-PRoot path unchanged.

Measured win (interleaved A/B, `bench/results/proot_free_ab.json`):

| Shape | Traced | Worker | Result |
|---|---|---|---|
| Standalone 30 s 1080p hardware encode | 15.46 s | **12.67 s** | **1.22×**, output byte-identical |
| 6 encodes, 3 parallel | 53.54 s | 51.45 s | neutral — only the solo shot is eligible |
| Remotion frame-mux encode (`image2pipe`) | 146.5 s | 151.1 s | noise; path stays traced (see below) |

The app-spawned worker lands in Android's **`background` cpuset** (cores 0–3,
the little cluster; ~1.5× slower per core here), and `am start` of the activity
is refused by background-start limits. So remote execution is **gated to a job
that is alone on the hardware encoder** (`runtime.active_encoder_jobs("hwenc")
== 0`), where PRoot's overhead dominates; with several encodes in flight the two
effects cancel and the traced path is kept. A Remotion-style `stdin` pipe is
also left traced: the shim would itself have to pump the pipe (traced) and the
worker's cpuset penalty offsets the saving — measured 0.95×, no gain.

### Why it is built this way (all measured on this device)

* **Runtime under `/data`, not in the repo.** The bionic linker picks its
  namespace config by executable path; only `/data` and `/tmp` map to the
  `[system]` section that links the APEX media libraries. The proot rootfs is
  not visible as a `/data` path.
* **No `LD_LIBRARY_PATH`.** It leaks into *system* library resolution (system
  libs then bind Termux's liblzma / Mesa EGL). Libraries resolve through
  `$ORIGIN`-relative RUNPATHs instead (shorter than Termux's original, so
  patchelf rewrites in place — growing it corrupted `libc++_shared.so`).
* **`libandroid-stub` removed.** Termux ships stub `libmediandk.so` /
  `libOpenSLES.so` that make every MediaCodec create call fail.
* **Binder thread pool.** Codec2 delivers work callbacks over binder; FFmpeg
  8.1 starts a 1-thread pool, but a decoder's `configure()` can start the
  default 15-thread pool first, turning FFmpeg's resize into
  `abort("Binder threadpool cannot be shrunk")`. `libom_binder_pool.so`
  (linked as `DT_NEEDED`) starts the pool at FFmpeg's size first.
* **Chromium on ANGLE `gles-egl`, not Vulkan.** This Linux/X11 ANGLE build
  requires `VK_KHR_xcb_surface`, which the Android Vulkan loader does not
  expose; the Termux wrapper ICD that does expose it crashes the GPU process
  (SIGSEGV). ANGLE over the Android EGL/GLES driver works and is verified.
* **CRF → CQ mapping.** The hardware encoder has no B-frames and different
  rate control; quality is held at VMAF parity-or-better with libx264
  `-preset medium` (see `bench/calibrate_cq.py`), which costs ~1.1–2.2× the
  bitrate. Size-constrained exports should pass an explicit `-b:v`.
* **Keyframe contracts.** The hardware I-frame interval is whole seconds and
  `-force_key_frames` is ignored, so `-g N` is moved to hardware only when it
  is an exact number of seconds at the output frame rate; everything else
  stays on libx264.

### Evaluated and rejected (kept on CPU, with evidence)

* **Zero-copy decode→GLES→encode engine** (`src/omgpu_zc_proto.c`):
  MediaCodec → `AImageReader`/`AHardwareBuffer` → `EGLImage` → encoder input
  surface works end-to-end on Mali, but the codec surface path serialises on
  this SoC: ~29–34 fps single job, 60 fps aggregate with 3 jobs, versus
  122 fps aggregate for CPU decode + buffer-mode hardware encode.
* **Hardware decode** through FFmpeg buffer mode: 75–100 fps vs 300 fps for
  multithreaded software decode, only ~28% less CPU.
* **FFmpeg Vulkan filters**: upload+download alone caps at ~89 fps
  (~8 ms/frame); blur/scale run slower on GPU than on CPU. libplacebo colour
  grading (chain compiled to a 3D LUT) halves CPU but produced two garbage
  leading frames and a ~34 dB colour-model mismatch versus FFmpeg's swscale
  path, so colour grading stays on CPU for correctness.
* **Debian Chromium + virgl/Venus bridge**: not pursued; the bionic Chromium
  reaches the vendor driver directly.
* **PRoot-free execution everywhere** (`lib/gpu/oop.py`): works and is used for
  solo file-output encodes, but the worker's `background` cpuset (cores 0–3)
  makes it a wash for parallel batches (1.04×) and for `stdin`-pipe encodes
  (0.95×), so it is deliberately gated, not applied globally.

## Policy and fallback

* `OM_GPU_MODE` (or `gpu.mode` in `config.yaml`): `auto` (default) | `cpu` |
  `require`. `require` never falls back — use it for acceptance runs.
* FFmpeg jobs: GPU output is validated (stream present, dimensions); on
  failure the *unmodified* CPU command is re-run. Only files the failed GPU
  attempt created or rewrote are removed; pre-existing outputs are never
  touched. Commands reading stdin / writing stdout cannot be replayed and run
  without fallback while the backend is healthy.
* Remotion: a render whose GPU process falls back to `--use-gl=disabled`
  mid-render is detected (`/proc` command line of that render's GPU process)
  and redone on the CPU path instead of shipping possibly incomplete frames.
* A backend with 3 GPU-specific failures in 15 minutes is skipped (health
  breaker). Hardware encoder sessions are bounded by cross-process slots.
* Every routed job is logged with the backend that actually ran it:
  `$OM_GPU_HOME/state/exec.jsonl`; tools also return
  `ToolResult.data["gpu_execution"]`. GPU jobs record `"oop": true` when the
  PRoot-free worker ran them.
* The PRoot-free worker is opt-out (`OM_GPU_OOP=0`) and never a dependency: a
  missing worker, a bad namespace map or a timeout returns to the traced path.
  When the worker's client socket closes it kills its own child, so an
  interrupted job cannot leave an orphan holding a codec slot.

## Install / rebuild

```bash
bash gpu_native/scripts/bootstrap_runtime.sh   # FFmpeg closure, native tools
bash gpu_native/scripts/bootstrap_browser.sh   # Chromium closure (ANGLE)
python -c "from lib.gpu import runtime; runtime.capabilities(refresh=True)"
```

Packages are fetched by their apt-index `Filename` and verified against the
index SHA256 (Termux apt refuses to run as proot root). Nothing is installed
into the global Termux prefix or the Debian system.

## Benchmarks and evidence

`bench/` holds the harnesses; `bench/results/` the recorded runs referenced by
the engineering report.
