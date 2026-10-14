"""GPU-native runtime discovery, hardware verification and execution policy.

OpenMontage runs inside a PRoot Debian (glibc) userland on Android, where
glibc processes can only reach software Vulkan/GL (llvmpipe, SwiftShader).
The genuine hardware paths — the Mali GPU (Vulkan / OpenGL ES) and the
MediaTek hardware video codecs (MediaCodec / Codec2) — are reachable only from
bionic (Android-linker) processes. The project-scoped native runtime under
``OM_GPU_HOME`` provides those processes (see ``gpu_native/README.md``).

This module answers three questions for the rest of OpenMontage:

* what hardware backends exist *and are verified genuine* (``capabilities()``)
* whether GPU-first execution is enabled (``mode()``)
* whether a backend is currently healthy enough to try (``healthy()``)

Verification is platform-attested, never inferred from names alone: codecs
must report ``HARDWARE_ACCELERATED`` through Android's MediaCodec info API,
and the Vulkan/EGL device must be a non-CPU device on the vendor driver with
``/dev/mali0`` open. Software implementations (llvmpipe, lavapipe,
SwiftShader, ``c2.android.*`` codecs) are reported but never treated as GPU.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOME = Path("/data/data/com.termux/files/home/.openmontage-gpu")

# Probe results are cached; a runtime rebuild (manifest change) invalidates.
_CAPS_TTL_S = 7 * 24 * 3600
_FAILED_PROBE_TTL_S = 600

# Software implementations that must never be reported as GPU acceleration.
_SOFTWARE_GPU_MARKERS = ("llvmpipe", "lavapipe", "swiftshader", "softpipe", "virgl")

MODES = ("auto", "cpu", "require")


def home() -> Path:
    return Path(os.environ.get("OM_GPU_HOME") or DEFAULT_HOME)


def state_dir() -> Path:
    d = home() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def native_ffmpeg() -> Path:
    return home() / "prefix" / "bin" / "ffmpeg"


def native_ffprobe() -> Path:
    return home() / "prefix" / "bin" / "ffprobe"


def probe_binary() -> Path:
    return home() / "bin" / "omgpu-probe"


def chromium_executable() -> Path:
    return home() / "browser" / "lib" / "chromium" / "headless_shell"


def chromium_launcher() -> Path:
    return REPO_ROOT / "gpu_native" / "shims" / "chromium-gpu"


def shim_dir() -> Path:
    return REPO_ROOT / "gpu_native" / "shims" / "path"


def mode() -> str:
    """GPU execution policy.

    ``auto``    (default) GPU-first; verified hardware backends are used by
                default and a failed GPU run falls back to the CPU path.
    ``cpu``     never use the native GPU runtime (A/B testing, debugging).
    ``require`` GPU or fail; no CPU fallback (acceptance testing).

    ``OM_GPU_MODE`` overrides ``config.yaml`` ``gpu.mode``.
    """
    env = (os.environ.get("OM_GPU_MODE") or "").strip().lower()
    if env in MODES:
        return env
    try:
        from lib.config_model import OpenMontageConfig

        cfg_mode = OpenMontageConfig.load().gpu.mode
        if cfg_mode in MODES:
            return cfg_mode
    except Exception:
        pass
    return "auto"


def runtime_installed() -> bool:
    return native_ffmpeg().is_file() and probe_binary().is_file()


def native_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Environment for bionic subprocesses.

    LD_LIBRARY_PATH / LD_PRELOAD from the glibc side must not leak into the
    Android linker: they make *system* libraries bind Termux copies (or fail).
    The native runtime resolves its own libraries through $ORIGIN RUNPATHs.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "LD_PRELOAD")}
    # Same font configuration as the Debian tools, so libass/drawtext/Chromium
    # text matches the CPU path.
    if Path("/etc/fonts/fonts.conf").exists():
        env.setdefault("FONTCONFIG_FILE", "/etc/fonts/fonts.conf")
    if extra:
        env.update(extra)
    return env


def _manifest_digest() -> str:
    parts = []
    for p in (home() / "manifest.json", native_ffmpeg(), probe_binary(), chromium_executable()):
        try:
            st = p.stat()
            parts.append(f"{p}:{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            parts.append(f"{p}:missing")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _is_software_name(name: str) -> bool:
    low = (name or "").lower()
    return any(m in low for m in _SOFTWARE_GPU_MARKERS)


def _run_probe(timeout: int = 60) -> dict[str, Any]:
    proc = subprocess.run(
        [str(probe_binary())], capture_output=True, text=True, timeout=timeout,
        env=native_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"omgpu-probe exit {proc.returncode}: {proc.stderr[-400:]}")
    return json.loads(proc.stdout)


def _ffmpeg_encoders(timeout: int = 30) -> set[str]:
    proc = subprocess.run(
        [str(native_ffmpeg()), "-hide_banner", "-encoders"], capture_output=True, text=True,
        timeout=timeout, env=native_env(),
    )
    names = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return names


def _summarize(raw: dict[str, Any], encoders: set[str]) -> dict[str, Any]:
    """Reduce the native probe to verified, typed hardware capabilities."""
    codecs: dict[str, dict[str, Any]] = {}
    mime_to_codec = {"video/avc": "h264", "video/hevc": "hevc", "video/x-vnd.on2.vp9": "vp9", "video/av01": "av1"}
    for entry in raw.get("mediacodec") or []:
        codec = mime_to_codec.get(entry.get("mime", ""))
        if not codec:
            continue
        role = entry.get("role")
        hw = bool(entry.get("available")) and entry.get("type") == "HARDWARE_ACCELERATED"
        codecs.setdefault(codec, {})[role] = {
            "component": entry.get("name"),
            "platform_type": entry.get("type"),
            "vendor": entry.get("vendor"),
            "max_instances": entry.get("max_instances"),
            "hardware": hw,
        }
    hw_encoders = {}
    for codec in ("h264", "hevc"):
        enc = codecs.get(codec, {}).get("encoder") or {}
        ffname = f"{codec}_mediacodec"
        if enc.get("hardware") and ffname in encoders:
            hw_encoders[codec] = {
                "ffmpeg_encoder": ffname,
                "component": enc["component"],
                "max_instances": enc.get("max_instances") or 1,
            }

    vk = raw.get("vulkan") or {}
    vk_dev = None
    for dev in vk.get("devices") or []:
        # VkPhysicalDeviceType: 1 integrated, 2 discrete, 4 CPU.
        if dev.get("type") in (1, 2) and not _is_software_name(dev.get("name", "")):
            vk_dev = dev
            break
    vulkan_ok = bool(vk_dev) and bool(vk.get("device_created")) and bool(vk.get("dev_mali0_fd"))

    egl = raw.get("egl") or {}
    gles_ok = bool(egl.get("available")) and not _is_software_name(egl.get("gl_renderer", "")) \
        and bool(egl.get("dev_mali0_fd"))

    return {
        "codecs": codecs,
        "hw_encoders": hw_encoders,
        "vulkan": {
            "verified": vulkan_ok,
            "device": vk_dev,
            "dev_mali0_fd": vk.get("dev_mali0_fd"),
            "vendor_driver_mapped": vk.get("vendor_driver_mapped"),
        },
        "gles": {
            "verified": gles_ok,
            "renderer": egl.get("gl_renderer"),
            "version": egl.get("gl_version"),
            "dev_mali0_fd": egl.get("dev_mali0_fd"),
        },
        "chromium_gpu": {
            "installed": chromium_executable().is_file() and chromium_launcher().is_file(),
            # Verified separately (needs a browser launch); see lib.gpu.remotion.
        },
    }


def capabilities(*, refresh: bool = False) -> dict[str, Any]:
    """Verified hardware capabilities of the native runtime (cached)."""
    cache = state_dir() / "capabilities.json"
    digest = _manifest_digest()
    if not refresh and cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            ttl = _CAPS_TTL_S if data.get("ok") else _FAILED_PROBE_TTL_S
            if data.get("digest") == digest and time.time() - data.get("probed_at", 0) < ttl:
                return data
        except (OSError, json.JSONDecodeError):
            pass

    data: dict[str, Any] = {"digest": digest, "probed_at": time.time(), "home": str(home())}
    if not runtime_installed():
        data.update(ok=False, error=f"native GPU runtime not installed at {home()}; "
                                      "run gpu_native/scripts/bootstrap_runtime.sh")
    else:
        try:
            raw = _run_probe()
            data.update(ok=True, **_summarize(raw, _ffmpeg_encoders()), raw_probe=raw)
        except Exception as exc:  # probe failures must not break CPU production
            data.update(ok=False, error=f"{type(exc).__name__}: {exc}")
    tmp = cache.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(cache)
    return data


def hw_encoder(codec: str) -> Optional[dict[str, Any]]:
    """Verified hardware encoder info for ``h264``/``hevc``, else None."""
    caps = capabilities()
    if not caps.get("ok"):
        return None
    return (caps.get("hw_encoders") or {}).get(codec)


# ---------------------------------------------------------------------------
# Health: a backend that keeps failing is skipped for a cool-down period so a
# broken driver/runtime degrades to CPU instead of failing every job twice.
# ---------------------------------------------------------------------------

_HEALTH_WINDOW_S = 900
_HEALTH_MAX_FAILURES = 3


def _health_file() -> Path:
    return state_dir() / "health.json"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_health() -> dict[str, Any]:
    try:
        return json.loads(_health_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def record_outcome(backend: str, ok: bool, detail: str = "") -> None:
    with _locked(state_dir() / "health.lock"):
        data = _read_health()
        entry = data.setdefault(backend, {"failures": [], "last_ok": None})
        now = time.time()
        if ok:
            entry["last_ok"] = now
            entry["failures"] = []
        else:
            entry["failures"] = [f for f in entry["failures"] if now - f["t"] < _HEALTH_WINDOW_S]
            entry["failures"].append({"t": now, "detail": detail[-300:]})
        tmp = _health_file().with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(_health_file())


def healthy(backend: str) -> bool:
    entry = _read_health().get(backend) or {}
    now = time.time()
    recent = [f for f in entry.get("failures", []) if now - f["t"] < _HEALTH_WINDOW_S]
    return len(recent) < _HEALTH_MAX_FAILURES


# ---------------------------------------------------------------------------
# Hardware codec session slots. The vendor encoder supports a bounded number
# of concurrent instances; parallel batch jobs queue for a slot instead of
# failing at configure() time.
# ---------------------------------------------------------------------------


@contextmanager
def codec_slot(kind: str, slots: int, *, wait_s: float = 600.0) -> Iterator[Optional[int]]:
    """Hold one of ``slots`` cross-process slots for ``kind``.

    Yields the slot index, or None if no slot became free within ``wait_s``
    (the caller should then use the CPU path).
    """
    lock_dir = state_dir() / "slots"
    lock_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_s
    handles = []
    try:
        while True:
            for i in range(max(1, slots)):
                fh = open(lock_dir / f"{kind}.{i}", "a+")
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    fh.close()
                    continue
                handles.append(fh)
                yield i
                return
            if time.monotonic() >= deadline:
                yield None
                return
            time.sleep(0.2)
    finally:
        for fh in handles:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


def media_parallelism() -> int:
    """Concurrent media jobs to schedule for independent encode work.

    On the GPU path each job needs ~1-2 CPU cores (demux/decode/filter) while
    encoding happens on the hardware encoder, so independent jobs (e.g.
    timeline segments) should overlap. On the CPU path libx264 already uses
    every core, so jobs stay sequential.
    """
    if mode() == "cpu" or not runtime_installed():
        return 1
    if hw_encoder("h264") is None or not healthy("mediacodec-encoder"):
        return 1
    try:
        n = int(os.environ.get("OM_GPU_MEDIA_JOBS", "3"))
    except ValueError:
        n = 3
    return max(1, min(n, 8))


def summary() -> dict[str, Any]:
    """Human/agent-facing summary used by preflight and tool get_info()."""
    caps = capabilities()
    out: dict[str, Any] = {"mode": mode(), "runtime_home": str(home()), "ok": bool(caps.get("ok"))}
    if not caps.get("ok"):
        out["error"] = caps.get("error")
        return out
    out["hardware_video_encoders"] = {k: v["component"] for k, v in (caps.get("hw_encoders") or {}).items()}
    out["hardware_video_decoders"] = {
        k: v["decoder"]["component"] for k, v in (caps.get("codecs") or {}).items()
        if (v.get("decoder") or {}).get("hardware")
    }
    out["software_only_codecs"] = sorted(
        f"{k}:{role}={info.get('component')}"
        for k, v in (caps.get("codecs") or {}).items()
        for role, info in v.items()
        if info.get("component") and not info.get("hardware")
    )
    vk = caps.get("vulkan") or {}
    out["vulkan_device"] = (vk.get("device") or {}).get("name") if vk.get("verified") else None
    gl = caps.get("gles") or {}
    out["gles_renderer"] = gl.get("renderer") if gl.get("verified") else None
    out["chromium_gpu_installed"] = (caps.get("chromium_gpu") or {}).get("installed")
    return out
