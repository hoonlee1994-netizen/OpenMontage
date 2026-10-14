"""A/B the PRoot-free worker against the traced path, interleaved.

Same workload, same hardware: only ``OM_GPU_OOP`` changes. Runs are
interleaved (off, on, on, off, ...) and reported as medians so background load
on this shared device affects both sides alike.

usage: oop_ab.py <out_dir> [--reps N] [--only single,batch,remotion]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

SHIM = str(REPO / "gpu_native" / "shims" / "path" / "ffmpeg")
SRC = str(REPO / "assets" / "signal-from-tomorrow-demo.mp4")
PY = str(REPO / ".venv" / "bin" / "python")
STATE = Path(os.environ.get("OM_GPU_HOME") or "/data/data/com.termux/files/home/.openmontage-gpu") / "state"


def _run(cmd, *, env=None, timeout=600.0):
    """Run a subprocess in its own session; kill the whole group on timeout."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except OSError:
            pass
        try:
            out, err = proc.communicate(timeout=20)
        except Exception:
            out, err = "", ""
        return -9, out, err, True


def telemetry_since(t: float) -> list:
    out = []
    try:
        for line in (STATE / "exec.jsonl").read_text().splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("t", 0) >= t and ev.get("kind") == "job" and ev.get("op") == "ffmpeg":
                out.append(ev)
    except OSError:
        pass
    return out


def frames(path: Path) -> int:
    p = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return int(p.stdout.strip().rstrip(","))
    except ValueError:
        return -1


def bench_single(out: Path, reps: int) -> list:
    recs = []
    for i in range(reps):
        order = ["0", "auto"] if i % 2 == 0 else ["auto", "0"]
        for mode in order:
            f = out / f"single_{mode}_{i}.mp4"
            t0 = time.time()
            env = dict(os.environ, OM_GPU_MODE="auto", OM_GPU_OOP=mode)
            t = time.monotonic()
            rc, _, _, timed_out = _run(
                [SHIM, "-y", "-v", "error", "-i", SRC, "-t", "30",
                 "-c:v", "libx264", "-crf", "23", "-preset", "medium",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(f)],
                env=env, timeout=240)
            wall = time.monotonic() - t
            evs = telemetry_since(t0)
            recs.append({"workload": "single_30s_1080p_hwenc", "rep": i, "oop": mode,
                         "wall_s": round(wall, 2), "rc": rc, "timed_out": timed_out,
                         "frames": frames(f) if f.exists() else -1,
                         "telemetry_oop": evs[-1].get("oop") if evs else None,
                         "backend": evs[-1].get("backend") if evs else None})
            print(json.dumps(recs[-1]), flush=True)
    return recs


def bench_batch(out: Path, reps: int) -> list:
    recs = []
    for i in range(reps):
        for mode in ("0", "auto"):
            d = out / f"batch_{mode}_{i}"
            env = dict(os.environ, OM_GPU_MODE="auto", OM_GPU_OOP=mode)
            t0 = time.time()
            t_start = time.monotonic()
            rc, stdout, _, timed_out = _run(
                [PY, str(HERE / "batch_transcode.py"), str(d), "6", "3"],
                env=env, timeout=420)
            wall = time.monotonic() - t_start
            try:
                agg = json.loads(stdout.strip().splitlines()[-1])
            except Exception:
                agg = {}
            evs = telemetry_since(t0)
            recs.append({"workload": "batch_6_jobs_par3", "rep": i, "oop": mode,
                         "wall_s": round(wall, 2), "rc": rc, "timed_out": timed_out,
                         "aggregate_fps": agg.get("aggregate_fps"),
                         "total_frames": agg.get("total_frames"),
                         "shots_using_worker": sum(1 for e in evs if e.get("oop")),
                         "shots": len(evs)})
            print(json.dumps(recs[-1]), flush=True)
    return recs


def bench_remotion(out: Path, reps: int) -> list:
    recs = []
    props = REPO / "remotion-composer" / "public" / "demo-props" / "world-in-numbers.json"
    for i in range(reps):
        for mode in ("0", "auto"):
            f = out / f"remotion_{mode}_{i}.mp4"
            env = dict(os.environ, OM_GPU_MODE="auto", OM_GPU_OOP=mode, E2E_REMOTION_TIMEOUT_MS="300000")
            t0 = time.time()
            t_start = time.monotonic()
            rc, stdout, stderr, timed_out = _run(
                [PY, str(HERE / "e2e_remotion.py"), str(props), str(f)],
                env=env, timeout=600)
            wall = time.monotonic() - t_start
            try:
                summary = json.loads(stdout)
            except Exception:
                summary = {}
            evs = telemetry_since(t0)
            enc = [e for e in evs if e.get("encoder")]
            recs.append({"workload": "remotion_render_1080p690f", "rep": i, "oop": mode,
                         "wall_s": round(wall, 2), "rc": rc, "timed_out": timed_out,
                         "success": summary.get("success"),
                         "gpu_execution": (summary.get("gpu_execution") or {}).get("backend"),
                         "encode_shots": len(enc), "shots_using_worker": sum(1 for e in enc if e.get("oop")),
                         "frames": frames(f) if f.exists() else -1})
            print(json.dumps(recs[-1]), flush=True)
    return recs


def summarize(recs: list) -> dict:
    out = {}
    for w in sorted({r["workload"] for r in recs}):
        sub = [r for r in recs if r["workload"] == w]
        off = [r["wall_s"] for r in sub if r["oop"] == "0"]
        on = [r["wall_s"] for r in sub if r["oop"] == "auto"]
        d = {"n": len(sub)}
        if off:
            d["traced_median_s"] = round(statistics.median(off), 2)
        if on:
            d["worker_median_s"] = round(statistics.median(on), 2)
        if off and on and statistics.median(off) > 0:
            d["speedup"] = round(statistics.median(off) / statistics.median(on), 3)
            d["saved_s"] = round(statistics.median(off) - statistics.median(on), 2)
        out[w] = d
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--only", default="single,batch,remotion")
    a = ap.parse_args()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    want = set(a.only.split(","))

    from lib.gpu import oop

    if not oop.ensure_worker(wait_s=10):
        print(json.dumps({"error": "worker unavailable (not under PRoot, or TermuxService refused)"}))
        return 2

    recs = []
    if "single" in want:
        recs += bench_single(out, a.reps)
    if "batch" in want:
        recs += bench_batch(out, a.reps)
    if "remotion" in want:
        recs += bench_remotion(out, a.reps)

    summary = summarize(recs)
    (out / "oop_ab.json").write_text(json.dumps({"records": recs, "summary": summary}, indent=1))
    print(json.dumps({"summary": summary}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
