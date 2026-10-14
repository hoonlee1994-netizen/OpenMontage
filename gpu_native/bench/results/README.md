# Recorded evidence (2026-10-14, Mali-G720 MC8 / MT6899, Android 16, PRoot Debian 12)

All runs on the real device, through the real OpenMontage tools unless noted.
The device was shared with other agent sessions (codex/opencode/claude) during
measurement; CPU and GPU runs were interleaved so that load affects both
sides. "media CPU" sums the CPU time of all processes doing the media work
(including the proot tracer), from `/proc/<pid>/stat` (`bench/sysmon.py`).

| File | What |
|---|---|
| `final_suite.json` | CPU (`OM_GPU_MODE=cpu`, pre-existing behaviour) vs GPU (`auto`) end-to-end runs — `bench/final_suite.py` |
| `parity_matrix.json` | Output-contract parity of 10 tool command shapes, CPU vs GPU — `bench/parity_matrix.py` |
| `hardware_evidence.json` | Platform codec info, FFmpeg's instantiated encoder, `/proc` maps/fds of the Chromium GPU process and the ncnn process during real work — `tools/collect_evidence.py` |
| `encoder_calibration.md`, `encoder_*.json` | libx264 CRF vs MediaCodec CQ quality calibration (VMAF/SSIM), GOP behaviour |
| `decode_sw_vs_mediacodec.json`, `filters_cpu_vs_vulkan.json` | Why decode and FFmpeg filters stay on CPU |
| `soak_summary.json` | Unattended mixed-queue soak with fault injection — `bench/soak.py` |

## Headline results (`final_suite.json`)

| Workload | CPU path | GPU path | Notes |
|---|---|---|---|
| Remotion render, 22 s 1080p30 demo (690 frames), `video_compose remotion_render` | **did not finish**: hit the tool's own 600 s render limit (1625 media core-s) | **71.6 s** incl. bundling (162 media core-s), 690 frames | CPU path completes only with a raised timeout (see report); bench-script CPU render alone: 379.8 s |
| Compose (6 cuts, 2 sources, letterbox, subtitles, 30.9 s out) | 67.0 s / 79.3 s, 325–422 core-s | 26.9 s / 34.2 s, 79–112 core-s | 2.3–2.5× faster, ~4× less CPU |
| Single transcode 1080p30 900 frames (`libx264 -crf 23` command) | 23.6 s / 42.1 s, 134–213 core-s, VMAF 93.65, 8.9 MB | 15.4 s / 16.7 s, 30–39 core-s, VMAF 95.88, 14.0 MB | hardware: higher quality, ~1.6× size |
| Super-resolution 480×270→1920×1080, RealESRGAN_x4plus | 154.9 s (PyTorch, 524 core-s) | 21.1 s (ncnn/Vulkan on Mali, 41 core-s) | 7.3×; GPU vs CPU output PSNR 49.2 dB, SSIM 0.998 |
| Sustained batch: 6 transcodes, 3 parallel (5400 frames) | 205.9 s, **26.3 fps** aggregate, 1300 core-s | 58.5 s, **93.4 fps** aggregate, 272 core-s | 3.55× throughput, 0 failures |

`compose_quality_gpu_vs_cpu` in `final_suite.json` (VMAF 9.95) is **superseded**:
that run predates the colour-range and font-path fixes and compared frames by
index. After the fixes, with timestamp alignment, GPU vs CPU compose output is
PSNR 46.6 dB (min 43.7) / VMAF 95.2, and `parity_matrix.json` shows equal frame
counts, start times and durations for all 10 command shapes (PSNR 45–50 dB).
