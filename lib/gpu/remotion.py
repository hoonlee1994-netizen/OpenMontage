"""GPU-native launch configuration for Remotion renders.

Remotion's cost on this device is dominated by Chromium rasterisation: the
Debian (glibc) Chromium can only use SwiftShader on the CPU. The project's
bionic Chromium (``gpu_native/shims/chromium-gpu``) runs ANGLE on the
Android GLES driver, i.e. on the Mali GPU. Frames are then encoded by the
MediaCodec hardware encoder through Remotion's ``--binaries-directory``
hook, whose ``ffmpeg`` routes through :mod:`lib.gpu.ffmpeg_exec` and falls
back to Remotion's own bundled FFmpeg for everything it does not accelerate.

Before the GPU browser is trusted it is verified once (cached): WebGL must
report the Mali renderer and read back a correct pixel. A browser that only
reaches SwiftShader is never reported as GPU.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from lib.gpu import runtime, telemetry

COMPOSER_DIR = runtime.REPO_ROOT / "remotion-composer"
_PROBE_SCRIPT = runtime.REPO_ROOT / "gpu_native" / "tools" / "browser_gpu_probe.cjs"
_VERIFY_TTL_S = 7 * 24 * 3600
_CPU_BROWSER_CANDIDATES = ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome")


def _compositor_dir() -> Optional[Path]:
    d = COMPOSER_DIR / "node_modules" / "@remotion" / "compositor-linux-arm64-gnu"
    return d if (d / "remotion").is_file() and (d / "ffmpeg").is_file() else None


def binaries_dir() -> Optional[Path]:
    """Remotion ``--binaries-directory`` with a GPU-dispatching ``ffmpeg``.

    Rebuilt idempotently: every file of the installed compositor package is
    symlinked (RPATH $ORIGIN resolves through the link to the real package),
    except ``ffmpeg``, which is a dispatcher script whose CPU path is
    Remotion's own bundled FFmpeg build.
    """
    comp = _compositor_dir()
    if comp is None:
        return None
    out = runtime.home() / "remotion-bin"
    out.mkdir(parents=True, exist_ok=True)
    for src in comp.iterdir():
        if src.name == "ffmpeg":
            continue
        link = out / src.name
        if link.is_symlink() and os.readlink(link) == str(src):
            continue
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(src)
    script = out / "ffmpeg"
    body = (
        "#!/bin/sh\n"
        "# Remotion encoder hook: video encodes -> MediaCodec hardware encoder,\n"
        "# everything else -> Remotion's bundled FFmpeg (lib/gpu/ffmpeg_exec.py).\n"
        f"export OM_GPU_REAL_FFMPEG='{comp / 'ffmpeg'}'\n"
        f"exec /usr/bin/python3 -I -S '{runtime.REPO_ROOT / 'gpu_native' / 'shims' / 'ffmpeg_dispatch.py'}' \"$@\"\n"
    )
    if not script.exists() or script.read_text() != body:
        tmp = script.with_suffix(".tmp")
        tmp.write_text(body)
        tmp.chmod(0o755)
        tmp.replace(script)
    return out


def cpu_browser() -> Optional[str]:
    for cand in _CPU_BROWSER_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def verify_browser(*, refresh: bool = False) -> dict[str, Any]:
    """Launch the GPU browser once through Remotion and confirm Mali rendering."""
    cache = runtime.state_dir() / "chromium_gpu.json"
    launcher = runtime.chromium_launcher()
    exe = runtime.chromium_executable()
    digest = f"{exe.stat().st_mtime_ns if exe.exists() else 0}:{launcher.stat().st_mtime_ns if launcher.exists() else 0}"
    if not refresh and cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("digest") == digest and time.time() - data.get("verified_at", 0) < _VERIFY_TTL_S:
                return data
        except (OSError, json.JSONDecodeError):
            pass
    data: dict[str, Any] = {"digest": digest, "verified_at": time.time(), "ok": False}
    if not (exe.is_file() and launcher.is_file() and _PROBE_SCRIPT.is_file()):
        data["error"] = "GPU Chromium runtime not installed"
    else:
        try:
            proc = subprocess.run(
                ["node", str(_PROBE_SCRIPT), str(launcher), "-"], capture_output=True, text=True,
                timeout=120, cwd=str(COMPOSER_DIR),
            )
            webgl = None
            for line in proc.stdout.splitlines():
                if line.startswith("WEBGL "):
                    webgl = json.loads(line[6:])
            data["webgl"] = webgl
            renderer = (webgl or {}).get("renderer") or ""
            pixel_ok = (webgl or {}).get("pixel") == [64, 128, 191, 255]
            hardware = "mali" in renderer.lower() and not any(
                m in renderer.lower() for m in ("swiftshader", "llvmpipe", "lavapipe"))
            data["ok"] = bool(webgl and hardware and pixel_ok)
            if not data["ok"]:
                data["error"] = f"GPU browser not verified (renderer={renderer!r}, pixel_ok={pixel_ok})"
        except Exception as exc:
            data["error"] = f"{type(exc).__name__}: {exc}"
    tmp = cache.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(cache)
    return data


def render_flags(*, gpu: bool) -> list[str]:
    """Extra ``npx remotion render`` flags for the chosen execution path."""
    flags: list[str] = []
    if gpu:
        flags.append(f"--browser-executable={runtime.chromium_launcher()}")
        bdir = binaries_dir()
        if bdir is not None:
            flags.append(f"--binaries-directory={bdir}")
        conc = os.environ.get("OM_REMOTION_GPU_CONCURRENCY")
        if conc:
            flags.append(f"--concurrency={conc}")
    else:
        browser = cpu_browser()
        if browser:
            flags.append(f"--browser-executable={browser}")
    return flags


def with_flags(base_cmd: list[str], flags: list[str]) -> list[str]:
    """Insert ``flags`` before the caller's first ``--option``.

    Keeps the positional arguments and the caller's own trailing options
    (e.g. ``--output=...``) exactly where the caller put them.
    """
    for i, tok in enumerate(base_cmd):
        if tok.startswith("--"):
            return [*base_cmd[:i], *flags, *base_cmd[i:]]
    return [*base_cmd, *flags]


def plan() -> dict[str, Any]:
    """Decide the Remotion execution path under the current GPU policy."""
    mode = runtime.mode()
    if mode == "cpu":
        return {"gpu": False, "reason": "OM_GPU_MODE=cpu"}
    if not runtime.runtime_installed():
        return {"gpu": False, "reason": "native GPU runtime not installed"}
    if not runtime.healthy("chromium-gpu"):
        return {"gpu": False, "reason": "GPU browser unhealthy (recent failures)"}
    v = verify_browser()
    if not v.get("ok"):
        return {"gpu": False, "reason": v.get("error") or "GPU browser not verified"}
    return {"gpu": True, "renderer": (v.get("webgl") or {}).get("renderer")}


def run_render(base_cmd: list[str], *, cwd: Path, timeout: Optional[int], runner) -> dict[str, Any]:
    """Run a Remotion CLI render GPU-first with automatic CPU fallback.

    ``base_cmd`` is the caller's ``npx remotion render ...`` command without
    browser/binaries flags; ``runner(cmd, timeout=, cwd=)`` executes it and
    raises on failure (``BaseTool.run_command``). Returns an execution record
    for the ToolResult.
    """
    t0 = time.time()
    decision = plan()
    attempts = []
    if decision["gpu"]:
        cmd = with_flags(base_cmd, render_flags(gpu=True))
        try:
            runner(cmd, timeout=timeout, cwd=cwd)
            runtime.record_outcome("chromium-gpu", True)
            rec = {"backend": "gpu", "renderer": decision.get("renderer"),
                   "wall_s": round(time.time() - t0, 2), "attempts": attempts}
            telemetry.record({"kind": "job", "op": "remotion_render", "backend": "gpu",
                              "device": decision.get("renderer"), "wall_s": rec["wall_s"]})
            return rec
        except subprocess.TimeoutExpired:
            raise
        except Exception as exc:  # GPU-specific failure: fall back below
            detail = (getattr(exc, "detail", "") or str(exc))[-600:]
            attempts.append({"backend": "gpu", "error": detail})
            if runtime.mode() == "require":
                runtime.record_outcome("chromium-gpu", False, detail)
                raise
            fallback_reason = f"GPU render failed: {detail[-300:]}"
    else:
        if runtime.mode() == "require":
            raise RuntimeError(f"OM_GPU_MODE=require but GPU Remotion path unavailable: {decision['reason']}")
        fallback_reason = decision["reason"]
    cmd = with_flags(base_cmd, render_flags(gpu=False))
    runner(cmd, timeout=timeout, cwd=cwd)
    if attempts:
        # The CPU path succeeded where the GPU path failed: GPU-specific.
        runtime.record_outcome("chromium-gpu", False, attempts[-1]["error"])
    rec = {"backend": "cpu", "fallback_reason": fallback_reason,
           "wall_s": round(time.time() - t0, 2), "attempts": attempts}
    telemetry.record({"kind": "job", "op": "remotion_render", "backend": "cpu",
                      "fallback_reason": fallback_reason, "wall_s": rec["wall_s"]})
    return rec
