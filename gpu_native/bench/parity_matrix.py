"""Output-contract parity: CPU (Debian FFmpeg 5.1 / libx264) vs GPU dispatch.

Each case is a command shape taken from an OpenMontage tool. It runs once with
OM_GPU_MODE=cpu and once with OM_GPU_MODE=require through the same PATH shim,
then compares the outputs' contract (frame count, start times, duration,
pixel format, colour range, audio presence) and their pts-aligned PSNR.

usage: python3 parity_matrix.py <out_dir>
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SHIM = str(REPO / "gpu_native" / "shims" / "path" / "ffmpeg")
NATIVE = "/data/data/com.termux/files/home/.openmontage-gpu/prefix/bin/ffmpeg"
A = str(REPO / "assets" / "signal-from-tomorrow-demo.mp4")
B = "/sdcard/Download/grok_video_2026-09-22-00-22-19.mp4"
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)


def prep():
    srt = OUT / "subs.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nParity subtitle one\n\n"
                   "2\n00:00:02,400 --> 00:00:04,100\nParity subtitle two\n", encoding="utf-8")
    vfr = OUT / "vfr.mkv"
    if not vfr.exists():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-t", "4",
                        "-vf", "setpts='if(lt(N,40),N/30,40/30+(N-40)/15)/TB'", "-fps_mode", "passthrough",
                        "-c:v", "libx264", "-crf", "18", str(vfr)], check=True)
    frames = OUT / "frames"
    if not frames.exists():
        frames.mkdir()
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "4", "-t", "2", "-i", A, "-vf", "scale=960:540",
                        str(frames / "frame_%06d.png")], check=True)
    logo = OUT / "logo.png"
    if not logo.exists():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=orange@0.8:s=320x120,format=rgba",
                        "-frames:v", "1", str(logo)], check=True)
    return srt, vfr, frames, logo


SRT, VFR, FRAMES, LOGO = prep()
GRADE = ("colorbalance=rs=0.08:gs=0.02:bs=-0.05:rh=0.06:gh=0.02:bh=-0.04,"
         "curves=all='0/0.03 0.25/0.22 0.5/0.50 0.75/0.78 1/0.97',eq=contrast=1.05:saturation=1.1")
PAD = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30"
CASES = {
    # video_compose._compose segment (30 fps source)
    "compose_segment_30fps": ["-y", "-ss", "8.5", "-t", "5.5", "-i", A, "-filter:v", PAD, "-c:v", "libx264",
                              "-crf", "23", "-preset", "medium", "-pix_fmt", "yuv420p", "-r", "30",
                              "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "{out}.mp4"],
    # same, 24 fps portrait AI clip -> 30 fps letterbox
    "compose_segment_24fps_portrait": ["-y", "-ss", "1", "-t", "5", "-i", B, "-filter:v", PAD, "-c:v", "libx264",
                                       "-crf", "23", "-preset", "medium", "-pix_fmt", "yuv420p", "-r", "30",
                                       "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "{out}.mp4"],
    # speed change segment
    "compose_segment_speed_1_25": ["-y", "-ss", "20", "-t", "6", "-i", A, "-filter:v", PAD + ",setpts=0.8*PTS",
                                   "-filter:a", "atempo=1.25", "-c:v", "libx264", "-crf", "23", "-preset", "medium",
                                   "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "192k", "{out}.mp4"],
    # burn_subtitles / final compose pass
    "subtitle_burn": ["-y", "-i", A, "-t", "5", "-vf", f"subtitles='{SRT}':force_style='FontName=Arial,FontSize=24'",
                      "-c:v", "libx264", "-crf", "23", "-preset", "medium", "-c:a", "copy", "{out}.mp4"],
    # variable-frame-rate source to mp4 (phone/screen recordings)
    "vfr_source_to_mp4": ["-y", "-i", str(VFR), "-c:v", "libx264", "-crf", "23", "{out}.mp4"],
    # color_grade tool
    "color_grade": ["-y", "-i", A, "-t", "4", "-vf", GRADE, "-c:v", "libx264", "-crf", "18", "-c:a", "copy", "{out}.mp4"],
    # video_compose overlay operation
    "overlay_image": ["-y", "-i", A, "-i", str(LOGO), "-t", "4", "-filter_complex",
                      "[1:v]format=rgba,colorchannelmixer=aa=0.8[ov];[0:v][ov]overlay=x=80:y=60:enable='between(t,0.5,3)'",
                      "-c:v", "libx264", "-crf", "23", "-c:a", "copy", "{out}.mp4"],
    # upscale reassembly (image sequence + original audio)
    "image_sequence_reassembly": ["-y", "-framerate", "30", "-i", str(FRAMES / "frame_%06d.png"), "-i", A,
                                  "-map", "0:v", "-map", "1:a?", "-c:v", "libx264", "-crf", "18", "-c:a", "copy",
                                  "-pix_fmt", "yuv420p", "-shortest", "{out}.mp4"],
    # video_trimmer accurate re-encode trim
    "trim_reencode": ["-y", "-ss", "3.3", "-i", A, "-t", "4.2", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                      "-c:a", "aac", "{out}.mp4"],
    # auto_reframe style crop to vertical
    "reframe_vertical": ["-y", "-i", A, "-t", "4", "-vf", "crop=608:1080:656:0,scale=1080:1920",
                         "-c:v", "libx264", "-crf", "23", "-preset", "fast", "-c:a", "copy", "{out}.mp4"],
}


def info(path: str) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-show_entries",
                        "stream=codec_type,pix_fmt,color_range,start_time,nb_read_packets,width,height:format=duration",
                        "-of", "json", path], capture_output=True, text=True)
    d = json.loads(p.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {"frames": int(v.get("nb_read_packets") or 0), "v_start": float(v.get("start_time") or 0),
            "a_start": float(a.get("start_time")) if a else None, "size": f"{v.get('width')}x{v.get('height')}",
            "pix_fmt": v.get("pix_fmt"), "range": v.get("color_range"),
            "duration": round(float(d.get("format", {}).get("duration") or 0), 3)}


def psnr(a: str, b: str) -> dict:
    # pts-aligned (framesync), both converted to the same pixel format.
    p = subprocess.run([NATIVE, "-hide_banner", "-nostats", "-i", a, "-i", b, "-lavfi",
                        "[0:v]format=yuv420p[x];[1:v]format=yuv420p[y];[x][y]psnr", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"average:([0-9.inf]+) min:([0-9.inf]+)", p.stderr)
    return {"avg": m.group(1), "min": m.group(2)} if m else {"error": p.stderr[-200:]}


rows = []
for name, argv in CASES.items():
    res = {"case": name}
    for mode in ("cpu", "require"):
        out = str(OUT / f"{name}_{mode}")
        cmd = [SHIM, *[a.replace("{out}", out) for a in argv]]
        p = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, OM_GPU_MODE=mode))
        res[mode] = {"rc": p.returncode, **(info(out + ".mp4") if p.returncode == 0 else {"err": p.stderr[-300:]})}
    if res["cpu"].get("rc") == 0 and res["require"].get("rc") == 0:
        c, g = res["cpu"], res["require"]
        res["contract_equal"] = all(c[k] == g[k] for k in ("frames", "size", "pix_fmt", "range")) and \
            abs(c["v_start"] - g["v_start"]) < 0.0005 and (c["a_start"] is None) == (g["a_start"] is None) and \
            abs(c["duration"] - g["duration"]) <= 0.034
        res["psnr_gpu_vs_cpu"] = psnr(str(OUT / f"{name}_require.mp4"), str(OUT / f"{name}_cpu.mp4"))
    rows.append(res)
    print(json.dumps({"case": name, "contract_equal": res.get("contract_equal"),
                      "cpu": {k: res["cpu"].get(k) for k in ("rc", "frames", "v_start", "duration", "range")},
                      "gpu": {k: res["require"].get(k) for k in ("rc", "frames", "v_start", "duration", "range")},
                      "psnr": res.get("psnr_gpu_vs_cpu")}), flush=True)
(OUT / "parity_matrix.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
print("cases:", len(rows), "contract-equal:", sum(bool(r.get("contract_equal")) for r in rows))
