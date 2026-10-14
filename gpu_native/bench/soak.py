"""Unattended reliability soak for the GPU-first execution paths.

A mixed production queue runs through the real OpenMontage tools with two
workers (OM_GPU_MODE=auto): Remotion renders, FFmpeg-path composes,
super-resolution of an image and a short video. While it runs, a fault
injector SIGKILLs one native hardware-encode process mid-job; the dispatcher
must fall back and the job must still produce a valid output. Every output is
validated (streams, frame counts, duration) and every job's backend is taken
from the execution log.

usage: python soak.py <out_dir>   (run with the project .venv python)
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.environ.setdefault("OM_GPU_MODE", "auto")

from lib.gpu import telemetry  # noqa: E402
from tools.enhancement.upscale import Upscale  # noqa: E402
from tools.video.video_compose import VideoCompose  # noqa: E402

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
A = str(REPO / "assets" / "signal-from-tomorrow-demo.mp4")
B = "/sdcard/Download/grok_video_2026-09-22-00-22-19.mp4"
DEMO = REPO / "remotion-composer" / "public" / "demo-props"


def probe(path: Path) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                        "stream=codec_type,codec_name,width,height,nb_read_frames:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True)
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError:
        return {}
    v = [s for s in d.get("streams", []) if s.get("codec_type") == "video"]
    a = [s for s in d.get("streams", []) if s.get("codec_type") == "audio"]
    return {"video": bool(v), "audio": bool(a), "frames": int(v[0].get("nb_read_frames") or 0) if v else 0,
            "size": f"{v[0]['width']}x{v[0]['height']}" if v else None,
            "duration": float(d.get("format", {}).get("duration") or 0)}


def remotion_job(name: str):
    def run():
        props = json.loads((DEMO / f"{name}.json").read_text())
        props.setdefault("renderer_family", "explainer-data")
        out = OUT / f"remotion_{name}.mp4"
        r = VideoCompose().execute({"operation": "remotion_render", "edit_decisions": props,
                                    "output_path": str(out), "remotion_timeout_ms": 300000})
        return r, out, {"min_frames": 600}
    return run


def compose_job(i: int, cuts):
    def run():
        out = OUT / f"compose_{i}.mp4"
        r = VideoCompose().execute({"operation": "compose", "edit_decisions": {"cuts": cuts},
                                    "output_path": str(out)})
        expect = round(sum((c["out_seconds"] - c["in_seconds"]) / c.get("speed", 1.0) for c in cuts) * 30)
        return r, out, {"approx_frames": expect}
    return run


def upscale_image_job():
    def run():
        src = OUT / "soak_sr_in.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "12", "-i", A, "-frames:v", "1",
                        "-vf", "scale=480:270", str(src)], check=True)
        out = OUT / "soak_sr_out.png"
        r = Upscale().execute({"input_path": str(src), "output_path": str(out)})
        return r, out, {"image": (1920, 1080)}
    return run


def upscale_video_job():
    def run():
        src = OUT / "soak_sr_clip.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "3", "-t", "1", "-i", A,
                        "-vf", "scale=240:136,fps=30", "-c:v", "libx264", "-crf", "18", "-c:a", "aac",
                        str(src)], check=True)
        out = OUT / "soak_sr_clip_x4.mp4"
        r = Upscale().execute({"input_path": str(src), "output_path": str(out)})
        return r, out, {"approx_frames": 30, "size": "960x544"}
    return run


JOBS = [
    ("remotion world-in-numbers", remotion_job("world-in-numbers")),
    ("compose 1", compose_job(1, [{"source": A, "in_seconds": 0, "out_seconds": 8},
                                  {"source": B, "in_seconds": 0, "out_seconds": 5}])),
    ("remotion focusflow-pitch", remotion_job("focusflow-pitch")),
    ("compose 2 (fault injected)", compose_job(2, [{"source": A, "in_seconds": 5, "out_seconds": 15},
                                                   {"source": A, "in_seconds": 20, "out_seconds": 28, "speed": 1.25}])),
    ("upscale image", upscale_image_job()),
    ("remotion code-to-screen", remotion_job("code-to-screen")),
    ("compose 3", compose_job(3, [{"source": B, "in_seconds": 2, "out_seconds": 9},
                                  {"source": A, "in_seconds": 1, "out_seconds": 7}])),
    ("upscale video 1s", upscale_video_job()),
    ("compose 4", compose_job(4, [{"source": A, "in_seconds": 0, "out_seconds": 30}])),
]

injected: dict = {}


def fault_injector(stop: threading.Event):
    """Kill the first native hardware encode that writes compose_2's segments."""
    while not stop.is_set() and not injected:
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                cl = open(f"/proc/{d}/cmdline", "rb").read().split(b"\0")
            except OSError:
                continue
            if cl and cl[0].endswith(b".openmontage-gpu/prefix/bin/ffmpeg") and \
                    any(b".compose_tmp" in a for a in cl) and any(b"-ss" == a for a in cl) and \
                    any(b"20" == a for a in cl):
                time.sleep(0.8)  # mid-encode
                try:
                    os.kill(int(d), signal.SIGKILL)
                    injected.update(pid=int(d), t=time.time(), cmd=[a.decode() for a in cl[:12]])
                except ProcessLookupError:
                    pass
                return
        stop.wait(0.1)


def main():
    t_start = time.time()
    stop = threading.Event()
    inj = threading.Thread(target=fault_injector, args=(stop,), daemon=True)
    inj.start()
    results = []

    def run_job(item):
        name, fn = item
        t0 = time.time()
        try:
            r, out, expect = fn()
            ok = r.success and out.exists()
            info = probe(out) if out.exists() and out.suffix == ".mp4" else {}
            if out.exists() and out.suffix == ".png":
                import cv2
                img = cv2.imread(str(out))
                info = {"image": (img.shape[1], img.shape[0]) if img is not None else None}
            valid = ok
            if "min_frames" in expect:
                valid = valid and info.get("frames", 0) >= expect["min_frames"] and info.get("audio")
            if "approx_frames" in expect:
                valid = valid and abs(info.get("frames", 0) - expect["approx_frames"]) <= 3
            if "image" in expect:
                valid = valid and info.get("image") == expect["image"]
            gx = (r.data or {}).get("gpu_execution")
            return {"job": name, "success": r.success, "valid_output": bool(valid), "wall_s": round(time.time() - t0, 1),
                    "output": info, "gpu_execution": gx, "error": (r.error or "")[-300:]}
        except Exception as exc:
            return {"job": name, "success": False, "valid_output": False, "wall_s": round(time.time() - t0, 1),
                    "error": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        for res in pool.map(run_job, JOBS):
            results.append(res)
            print(json.dumps({k: res[k] for k in ("job", "success", "valid_output", "wall_s")}), flush=True)
    stop.set()
    events = telemetry.since(t_start)
    summary = {
        "jobs": len(results),
        "succeeded": sum(r["success"] for r in results),
        "valid_outputs": sum(r["valid_output"] for r in results),
        "total_wall_s": round(time.time() - t_start, 1),
        "fault_injected": injected or None,
        "backends": telemetry.summarize(events)["by_backend"],
        "fallback_events": [e for e in events if e.get("fallback_reason")][:10],
        "results": results,
    }
    (OUT / "soak_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("jobs", "succeeded", "valid_outputs", "total_wall_s",
                                               "fault_injected", "backends")}, indent=1))


if __name__ == "__main__":
    main()
