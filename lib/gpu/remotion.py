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
import threading
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
    digest = (f"{exe.stat().st_mtime_ns if exe.exists() else 0}:"
              f"{launcher.stat().st_mtime_ns if launcher.exists() else 0}:"
              f"{os.environ.get('OM_CHROMIUM_ANGLE', 'gles-egl')}")
    if not refresh and cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            # A failed verification is retried soon; a pass is trusted longer.
            ttl = _VERIFY_TTL_S if data.get("ok") else 600
            if data.get("digest") == digest and time.time() - data.get("verified_at", 0) < ttl:
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
        # With rasterisation on the GPU the per-tab CPU cost drops ~5x, so
        # more tabs overlap usefully (measured best at 6 of 8 cores; see
        # gpu_native/bench). A caller's own --concurrency comes later on the
        # command line and wins.
        conc = os.environ.get("OM_REMOTION_GPU_CONCURRENCY", "6")
        if conc and conc != "default":
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


class _GpuProcessWatch:
    """Watch the GPU processes of one render (identified by OM_RENDER_ID).

    If Chromium's Mali GPU process fails, Chromium relaunches it with
    ``--use-gl=disabled`` (software compositing) or SwiftShader and keeps
    rendering on the CPU. That shows up in the GPU process command line, which
    this watcher records, so such a render is reported as degraded rather
    than as GPU.
    """

    def __init__(self, render_id: str):
        self.render_id = render_id.encode()
        self.seen: dict[str, list[str]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _scan(self) -> None:
        for d in os.listdir("/proc"):
            if not d.isdigit() or d in self.seen:
                continue
            try:
                with open(f"/proc/{d}/cmdline", "rb") as fh:
                    argv = fh.read().split(b"\0")
                if not any(a == b"--type=gpu-process" for a in argv):
                    continue
                with open(f"/proc/{d}/environ", "rb") as fh:
                    if b"OM_RENDER_ID=" + self.render_id not in fh.read().split(b"\0"):
                        continue
            except OSError:
                continue
            self.seen[d] = [a.decode("utf-8", "replace") for a in argv
                            if a.startswith((b"--use-gl", b"--use-angle", b"--disable-gpu"))]

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            self._scan()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        self._scan()

    def verdict(self) -> tuple[str, list[str]]:
        procs = list(self.seen.values())
        software = [f for p in procs for f in p if f in ("--use-gl=disabled", "--disable-gpu")
                    or "swiftshader" in f]
        hardware = [p for p in procs if "--use-angle=gles-egl" in p and "--use-gl=angle" in p]
        if software:
            return "gpu-degraded", sorted(set(software))
        if not hardware:
            return "gpu-unverified", ["no Mali GPU process observed for this render"]
        return "gpu", []


_env_lock = threading.Lock()


class _DegradedRender(RuntimeError):
    def __init__(self, problems: str, processes: list):
        super().__init__(problems)
        self.processes = processes


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
        render_id = f"{os.getpid()}-{time.time_ns()}"
        try:
            # Child processes (npx -> node -> Chromium -> GPU process) inherit
            # OM_RENDER_ID, which ties the observed GPU processes to this render.
            with _env_lock:
                prev = os.environ.get("OM_RENDER_ID")
                os.environ["OM_RENDER_ID"] = render_id
            try:
                with _GpuProcessWatch(render_id) as watch:
                    runner(cmd, timeout=timeout, cwd=cwd)
            finally:
                with _env_lock:
                    if prev is None:
                        os.environ.pop("OM_RENDER_ID", None)
                    else:
                        os.environ["OM_RENDER_ID"] = prev
            backend, problems = watch.verdict()
            if backend == "gpu-degraded":
                # Frames after the GPU process failure were composited on the
                # CPU without a software rasteriser, so WebGL content may be
                # missing: never ship it. Redo the render on the CPU path.
                raise _DegradedRender("; ".join(problems), list(watch.seen.values())[:6])
            runtime.record_outcome("chromium-gpu", True)
            rec = {"backend": backend, "renderer": decision.get("renderer"),
                   "wall_s": round(time.time() - t0, 2), "attempts": attempts,
                   "gpu_processes": list(watch.seen.values())[:6]}
            if problems:
                rec["warning"] = "GPU process not observed for this render: " + "; ".join(problems)
            telemetry.record({"kind": "job", "op": "remotion_render", "backend": backend,
                              "device": decision.get("renderer"), "wall_s": rec["wall_s"],
                              **({"problems": problems} if problems else {})})
            return rec
        except subprocess.TimeoutExpired:
            raise
        except Exception as exc:  # GPU-specific failure: fall back below
            detail = (getattr(exc, "detail", "") or str(exc))[-600:]
            if isinstance(exc, _DegradedRender):
                detail = f"GPU process fell back to CPU compositing mid-render ({exc})"
                attempts.append({"backend": "gpu-degraded", "error": detail, "gpu_processes": exc.processes})
                telemetry.record({"kind": "job", "op": "remotion_render", "backend": "gpu-degraded",
                                  "device": decision.get("renderer"), "problems": str(exc)})
            else:
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
