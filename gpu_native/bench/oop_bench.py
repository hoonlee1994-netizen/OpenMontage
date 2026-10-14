"""A/B: the same GPU-native workload inside PRoot vs. outside PRoot.

Runs identical native (bionic) FFmpeg command lines on the verified MediaCodec
hardware encoder twice — once as a child of the PRoot tracer, once in an
app-spawned proot-free process (``oop.py``) — and reports the wall-clock
difference.  This isolates PRoot's contribution from the GPU work itself.

usage: oop_bench.py <out_dir> [--reps N] [--seconds S] [--only NAME]
workloads: version encode
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import oop  # noqa: E402
from lib.gpu import ffmpeg_args  # noqa: E402

NATIVE_FFMPEG = "/data/data/com.termux/files/home/.openmontage-gpu/prefix/bin/ffmpeg"
SRC_GUEST = str(REPO / "assets" / "signal-from-tomorrow-demo.mp4")


def hostify(argv: list[str]) -> list[str]:
    """Map absolute guest paths inside an argv onto host paths (out of proot)."""
    out = []
    for a in argv:
        if a.startswith("/") and os.path.lexists(oop.guest_to_host(a)):
            out.append(oop.guest_to_host(a))
        else:
            out.append(a)
    return out


def timed_in_proot(cmd: list[str]) -> float:
    t0 = time.monotonic()
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return time.monotonic() - t0


def timed_out_of_proot(cmd: list[str], cwd: str) -> tuple[float, int]:
    r = oop.run(hostify(cmd), cwd=cwd, timeout=600)
    return r.wall_s, r.rc


def encode_argv(out_guest: str, seconds: float) -> list[str]:
    """The GPU command the shim would build for a tool-style x264 encode."""
    cpu = ["-y", "-v", "error", "-i", SRC_GUEST, "-t", str(seconds),
           "-c:v", "libx264", "-crf", "23", "-preset", "medium", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", out_guest]
    gpu, plans = ffmpeg_args.translate(cpu, available={"h264", "hevc"}, reference="legacy")
    assert gpu is not None and any(p.action == "gpu" for p in plans), plans
    return [NATIVE_FFMPEG, *gpu]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--only", default=None)
    a = ap.parse_args()

    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    only = set(a.only.split(",")) if a.only else {"version", "encode"}

    if not oop.available():
        print(json.dumps({"error": "out-of-proot channel unavailable"}))
        return 2

    records = []

    if "version" in only:
        for i in range(a.reps):
            r_in, r_out = [], []
            for _ in range(3):
                r_in.append(timed_in_proot([NATIVE_FFMPEG, "-version"]))
            for _ in range(3):
                w, rc = timed_out_of_proot([NATIVE_FFMPEG, "-version"], str(REPO))
                r_out.append(w)
            rec = {"workload": "native_ffmpeg_-version", "rep": i,
                   "in_proot_min_s": round(min(r_in), 3),
                   "out_of_proot_min_s": round(min(r_out), 3), "out_rc": rc}
            rec["saved_s"] = round(rec["in_proot_min_s"] - rec["out_of_proot_min_s"], 3)
            rec["speedup"] = round(rec["in_proot_min_s"] / rec["out_of_proot_min_s"], 2)
            records.append(rec)
            print(json.dumps(rec), flush=True)

    if "encode" in only:
        d = out / "media"
        d.mkdir(parents=True, exist_ok=True)
        in_guest = str(out / f"enc_in_{int(a.seconds)}s.mp4")
        subprocess.run([NATIVE_FFMPEG, "-y", "-v", "error", "-i", SRC_GUEST, "-t", str(a.seconds),
                        "-c", "copy", in_guest], check=False)
        for i in range(a.reps):
            out_in = str(out / f"enc_inproot_{i}.mp4")
            out_oop = str(out / f"enc_oop_{i}.mp4")
            argv_in = encode_argv(out_in, a.seconds)
            argv_oop = encode_argv(out_oop, a.seconds)
            w_in = timed_in_proot(argv_in)
            w_out, rc = timed_out_of_proot(argv_oop, str(REPO))
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                 "-show_entries", "stream=nb_read_frames,codec_name,width,height",
                 "-of", "json", str(out_oop)], capture_output=True, text=True)
            rec = {"workload": f"mediacodec_encode_{a.seconds:g}s", "rep": i,
                   "in_proot_s": round(w_in, 2), "out_of_proot_s": round(w_out, 2),
                   "out_rc": rc, "out_stream": proc.stdout.strip()}
            rec["saved_s"] = round(w_in - w_out, 2)
            rec["speedup"] = round(w_in / w_out, 2) if w_out else None
            records.append(rec)
            print(json.dumps(rec), flush=True)

    (out / "oop_bench.json").write_text(json.dumps(records, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
