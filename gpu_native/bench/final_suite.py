"""Final CPU-vs-GPU evidence suite (representative production workloads).

Runs each workload on the CPU path (OM_GPU_MODE=cpu, i.e. the pre-existing
OpenMontage behaviour) and the GPU path (OM_GPU_MODE=auto), interleaved
CPU/GPU so background load on the shared device affects both sides alike.
Whole-device CPU is attributed per process with sysmon.py. Outputs are
validated (streams, frame counts, duration) and quality-compared.

usage: python3 final_suite.py <out_dir> [workload ...]
workloads: remotion compose encode upscale batch
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
from harness import probe, quality  # noqa: E402

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
WANT = set(sys.argv[2:]) or {"remotion", "compose", "encode", "upscale", "batch"}
PY = str(REPO / ".venv" / "bin" / "python")
SYSMON = str(HERE / "sysmon.py")
SRC_A = str(REPO / "assets" / "signal-from-tomorrow-demo.mp4")
RESULTS = OUT / "final_suite.jsonl"


def frames(path: Path):
    p = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_read_frames,codec_name,width,height:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True)
    try:
        d = json.loads(p.stdout)
        st = d["streams"][0]
        return {"frames": int(st["nb_read_frames"]), "codec": st["codec_name"],
                "size": f"{st['width']}x{st['height']}", "duration": float(d["format"]["duration"])}
    except Exception:
        return None


def measured(tag: str, mode: str, cmd: list[str], env_extra=None, cwd=None):
    env = dict(os.environ, OM_GPU_MODE=mode, **(env_extra or {}))
    mon = OUT / f"mon_{tag}_{mode}_{int(time.time())}.json"
    t0 = time.time()
    proc = subprocess.run([sys.executable, SYSMON, str(mon), "--", *cmd], capture_output=True, text=True,
                          env=env, cwd=cwd)
    wall = time.time() - t0
    try:
        m = json.loads(mon.read_text())
    except Exception:
        m = {}
    media = {k: v for k, v in (m.get("by_process") or {}).items()
             if k not in ("claude", "codex", "opencode", "grok", "2.1.296", "codex-code-mode", "bash", "git")}
    return {"tag": tag, "mode": mode, "rc": proc.returncode, "wall_s": round(wall, 2),
            "device_cpu_core_s": m.get("cpu_core_s"), "media_cpu_core_s": round(sum(media.values()), 1),
            "by_process": media, "stdout_tail": proc.stdout[-1500:], "stderr_tail": proc.stderr[-800:]}


def emit(rec):
    with open(RESULTS, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(json.dumps({k: rec[k] for k in rec if k not in ("stdout_tail", "stderr_tail", "by_process")}), flush=True)


# --- W1: Remotion render (22 s, 1080p30, 690 frames) through the real tool ---
if "remotion" in WANT:
    props = REPO / "remotion-composer" / "public" / "demo-props" / "world-in-numbers.json"
    for mode in ("auto", "cpu"):
        out = OUT / f"remotion_{mode}.mp4"
        rec = measured("remotion_render", mode,
                       [PY, str(HERE / "e2e_remotion.py"), str(props), str(out)],
                       env_extra={"E2E_REMOTION_TIMEOUT_MS": "300000"})
        rec["output"] = frames(out) if out.exists() else None
        emit(rec)
    a, c = OUT / "remotion_auto.mp4", OUT / "remotion_cpu.mp4"
    if a.exists() and c.exists():
        emit({"tag": "remotion_quality_gpu_vs_cpu", "mode": "compare", "quality": quality(a, c)})

# --- W2: compose (FFmpeg path: 6 cuts, 2 sources, letterbox, subtitles) ---
if "compose" in WANT:
    for rep in range(2):
        for mode in ("cpu", "auto") if rep % 2 == 0 else ("auto", "cpu"):
            out = OUT / f"compose_{mode}_{rep}.mp4"
            rec = measured("compose", mode, [PY, str(HERE / "e2e_compose.py"), str(out)])
            rec["rep"] = rep
            rec["output"] = frames(out) if out.exists() else None
            emit(rec)
    a, c = OUT / "compose_auto_0.mp4", OUT / "compose_cpu_0.mp4"
    if a.exists() and c.exists():
        emit({"tag": "compose_quality_gpu_vs_cpu", "mode": "compare", "quality": quality(a, c)})

# --- W3: single transcode 1080p30 900 frames (tool-style libx264 crf23 command) ---
if "encode" in WANT:
    shim = str(REPO / "gpu_native" / "shims" / "path" / "ffmpeg")
    for rep in range(2):
        for mode in ("cpu", "auto") if rep % 2 == 0 else ("auto", "cpu"):
            out = OUT / f"encode_{mode}_{rep}.mp4"
            rec = measured("encode_1080p_crf23", mode,
                           [shim, "-y", "-v", "error", "-i", SRC_A, "-c:v", "libx264", "-crf", "23",
                            "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "copy", str(out)])
            rec["rep"] = rep
            rec["output"] = frames(out) if out.exists() else None
            if out.exists():
                rec["quality_vs_source"] = quality(out, Path(SRC_A))
                rec["size_bytes"] = out.stat().st_size
            emit(rec)

# --- W4: super-resolution 480x270 -> 1920x1080 through the upscale tool ---
if "upscale" in WANT:
    src = OUT / "sr_in270.png"
    if not src.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "5", "-i", SRC_A, "-frames:v", "1",
                        "-vf", "scale=480:270", str(src)], check=True)
    for mode in ("auto", "cpu"):
        out = OUT / f"sr_{mode}.png"
        rec = measured("upscale_x4plus_image", mode, [PY, str(HERE / "e2e_upscale.py"), str(src), str(out)])
        emit(rec)
    a, c = OUT / "sr_auto.png", OUT / "sr_cpu.png"
    if a.exists() and c.exists():
        emit({"tag": "upscale_quality_gpu_vs_cpu", "mode": "compare", "quality": quality(a, c)})

# --- W5: sustained batch: 6 transcodes, 3 at a time, aggregate throughput ---
if "batch" in WANT:
    script = HERE / "batch_transcode.py"
    for mode in ("cpu", "auto"):
        rec = measured("batch_6x_transcode_par3", mode, [sys.executable, str(script), str(OUT / f"batch_{mode}"), "6", "3"])
        emit(rec)
