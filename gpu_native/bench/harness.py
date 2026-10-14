"""Measurement harness for GPU-native vs CPU media workloads.

Each run records wall-clock time, child CPU time (user+sys, from
RUSAGE_CHILDREN deltas), peak child RSS, output frame count, and optionally
VMAF/PSNR/SSIM against a reference. CPU time per output frame is the number
that matters for sustained throughput: it is what limits how many jobs the
8-core SoC can run concurrently.
"""
from __future__ import annotations

import json
import os
import re
import resource
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

OM_GPU_HOME = Path(os.environ.get("OM_GPU_HOME", "/data/data/com.termux/files/home/.openmontage-gpu"))
NATIVE_FFMPEG = str(OM_GPU_HOME / "prefix" / "bin" / "ffmpeg")
NATIVE_FFPROBE = str(OM_GPU_HOME / "prefix" / "bin" / "ffprobe")


@dataclass
class RunResult:
    name: str
    cmd: list[str]
    returncode: int
    wall_s: float
    cpu_user_s: float
    cpu_sys_s: float
    max_rss_mb: float
    frames: Optional[int] = None
    fps: Optional[float] = None
    cpu_s_per_frame: Optional[float] = None
    output_bytes: Optional[int] = None
    bitrate_kbps: Optional[float] = None
    quality: dict = field(default_factory=dict)
    note: str = ""
    stderr_tail: str = ""

    @property
    def cpu_s(self) -> float:
        return self.cpu_user_s + self.cpu_sys_s


def run(name: str, cmd: list[str], *, env: Optional[dict] = None, timeout: int = 1800,
        output: Optional[Path] = None, note: str = "") -> RunResult:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                          env=env, timeout=timeout)
    wall = time.monotonic() - t0
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    res = RunResult(
        name=name,
        cmd=cmd,
        returncode=proc.returncode,
        wall_s=round(wall, 3),
        cpu_user_s=round(after.ru_utime - before.ru_utime, 3),
        cpu_sys_s=round(after.ru_stime - before.ru_stime, 3),
        # ru_maxrss is the max over all waited children so far; good enough
        # when runs are executed one at a time in a fresh harness process.
        max_rss_mb=round(after.ru_maxrss / 1024, 1),
        note=note,
        stderr_tail="\n".join((proc.stderr or "").strip().splitlines()[-6:]),
    )
    if output is not None and output.exists() and proc.returncode == 0:
        info = probe(output)
        res.frames = info.get("frames")
        res.output_bytes = output.stat().st_size
        dur = info.get("duration") or 0
        if dur:
            res.bitrate_kbps = round(res.output_bytes * 8 / dur / 1000, 1)
        if res.frames:
            res.fps = round(res.frames / wall, 2)
            res.cpu_s_per_frame = round(res.cpu_s / res.frames, 5)
    return res


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=nb_read_packets,width,height,codec_name,r_frame_rate:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {}
    st = (data.get("streams") or [{}])[0]
    return {
        "frames": int(st["nb_read_packets"]) if st.get("nb_read_packets") else None,
        "width": st.get("width"),
        "height": st.get("height"),
        "codec": st.get("codec_name"),
        "duration": float(data.get("format", {}).get("duration") or 0),
    }


def quality(distorted: Path, reference: Path, *, scale: Optional[str] = None,
            threads: int = 8) -> dict:
    """VMAF + PSNR + SSIM of `distorted` vs `reference` (native ffmpeg libvmaf).

    `scale` ("WxH") rescales both to the same geometry when they differ.
    Quality evaluation is measurement tooling, not production work.
    """
    pre = f"scale={scale}:flags=bicubic," if scale else ""
    graph = (
        f"[0:v]{pre}setpts=PTS-STARTPTS,format=yuv420p,split=3[d0][d1][d2];"
        f"[1:v]{pre}setpts=PTS-STARTPTS,format=yuv420p,split=3[r0][r1][r2];"
        f"[d0][r0]libvmaf=n_threads={threads}:log_fmt=json:log_path=/dev/null;"
        f"[d1][r1]psnr;[d2][r2]ssim"
    )
    proc = subprocess.run(
        [NATIVE_FFMPEG, "-hide_banner", "-nostats", "-i", str(distorted), "-i", str(reference),
         "-lavfi", graph, "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    text = proc.stderr
    q: dict = {}
    m = re.search(r"VMAF score: ([0-9.]+)", text)
    if m:
        q["vmaf"] = round(float(m.group(1)), 2)
    m = re.search(r"PSNR .*? average:([0-9.inf]+)", text)
    if m:
        q["psnr"] = m.group(1)
    m = re.search(r"SSIM .*? All:([0-9.]+)", text)
    if m:
        q["ssim"] = round(float(m.group(1)), 5)
    if not q:
        q["error"] = "\n".join(text.strip().splitlines()[-4:])
    return q


def save(results: list[RunResult], path: Path) -> None:
    rows = []
    for r in results:
        d = asdict(r)
        d["cpu_s"] = round(r.cpu_s, 3)
        rows.append(d)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def table(results: list[RunResult]) -> str:
    hdr = f"{'name':44} {'rc':>3} {'wall_s':>8} {'fps':>8} {'cpu_s':>8} {'cpu/frm':>8} {'kbps':>8} {'vmaf':>6} {'ssim':>7}"
    lines = [hdr, "-" * len(hdr)]
    for r in results:
        lines.append(
            f"{r.name[:44]:44} {r.returncode:>3} {r.wall_s:>8.2f} {r.fps or 0:>8.2f} {r.cpu_s:>8.2f} "
            f"{(r.cpu_s_per_frame or 0):>8.4f} {r.bitrate_kbps or 0:>8.0f} "
            f"{r.quality.get('vmaf', 0):>6} {r.quality.get('ssim', 0):>7}"
        )
    return "\n".join(lines)
