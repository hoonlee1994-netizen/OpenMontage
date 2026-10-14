# Hardware encoder calibration (c2.mtk.avc.encoder vs libx264)

Sources (real content, not synthetic):

* **A** `assets/signal-from-tomorrow-demo.mp4` — 1920x1080, 30 fps, 30 s, 900 frames (OpenMontage production render)
* **B** a 416x752, 24 fps, 10 s AI-generated clip (typical generated-asset input)

Quality = VMAF / SSIM against the decoded source (libvmaf in the native FFmpeg
8.1). CPU = child CPU time of the encode process. The CPU encodes run on the
Debian FFmpeg 5.1 libx264 `-preset medium` (OpenMontage's default); the
hardware encodes run `h264_mediacodec -bitrate_mode cq -global_quality Q -g 150
-profile:v high`. Raw data: `encoder_cq_vs_crf.json`, `encoder_crf_vs_bitrate.json`,
`encoder_gop.json`.

| Source | Encoder | Setting | kbps | VMAF | SSIM | CPU core-s | fps |
|---|---|---|---:|---:|---:|---:|---:|
| A | libx264 medium | CRF 18 | 4177 | 96.25 | 0.99712 | 237.5 | 23.1 |
| A | libx264 medium | CRF 23 | 2038 | 93.65 | 0.99482 | 140.8 | 36.1 |
| A | libx264 medium | CRF 28 | 995 | 89.29 | 0.99168 | 116.4 | 37.6 |
| A | MTK hardware | CQ 72 | 1448 | 92.12 | 0.98387 | 16.7 | 66.9 |
| A | MTK hardware | CQ 78 | 1912 | 93.76 | 0.98709 | 16.5 | 56.2 |
| A | MTK hardware | CQ 90 | 3441 | 95.88 | 0.99186 | 17.1 | 94.7 |
| A | MTK hardware | CQ 98 | 4557 | 96.51 | 0.99348 | 22.2 | 78.7 |
| A | MTK hardware | CQ 100 | 5106 | 96.77 | 0.99411 | 23.3 | 70.7 |
| B | libx264 medium | CRF 18 | 1523 | 99.00 | 0.99117 | 12.2 | 63.9 |
| B | libx264 medium | CRF 23 | 821 | 97.04 | 0.98459 | 10.6 | 79.3 |
| B | libx264 medium | CRF 28 | 458 | 91.56 | 0.97369 | 9.3 | 94.8 |
| B | MTK hardware | CQ 72 | 812 | 94.96 | 0.97694 | 2.4 | 51.8 |
| B | MTK hardware | CQ 78 | 1064 | 97.07 | 0.98162 | 2.6 | 56.9 |
| B | MTK hardware | CQ 90 | 1781 | 98.86 | 0.98789 | 2.4 | 55.3 |
| B | MTK hardware | CQ 98 | 2337 | 99.23 | 0.98999 | 3.9 | 51.4 |

(CPU core-s for the hardware rows is mostly *software decode* of the source;
the encode itself runs on the dedicated encoder.)

## Mapping used by `lib/gpu/ffmpeg_args.CQ_TABLE`

Rule: the CQ level whose VMAF is ≥ libx264's on **both** sources, raised
where needed to keep the luma-SSIM gap small (the MTK encoder preserves chroma
as well as x264 but loses some fine luma structure at equal VMAF).

| libx264 CRF | CQ | VMAF A / B (x264 → hardware) |
|---:|---:|---|
| 16 | 100 | — |
| 18 | 98 | 96.25 → 96.51 / 99.00 → 99.23 |
| 23 (default) | 90 | 93.65 → 95.88 / 97.04 → 98.86 |
| 28 | 75 | 89.29 → ≥92.1 / 91.56 → ≥94.96 |
| 35 | 55 | — |

Cost: the hardware encoder has no B-frames (configure fails with `-bf > 0`)
and coarser rate control, so equal-or-better quality takes **~1.1–2.2× the
bitrate** of x264 medium. Quality is held constant, not file size. Size-capped
exports should pass an explicit `-b:v`, which is honoured (VBR).

## Keyframe structure (`encoder_gop.json`)

The MediaCodec I-frame interval is whole seconds: `-g 12/30/45` at 30 fps all
produce a keyframe every 30 frames, `-g 60` every 60, `-g 250` every 240.
`-force_key_frames` is ignored. The translator therefore moves `-g N` to
hardware only when N is an exact whole number of seconds at the output frame
rate and keeps `-force_key_frames` and fractional GOPs on libx264.
