"""FFmpeg command-line parsing and GPU-native translation (pure functions).

Tools across OpenMontage build CPU-oriented FFmpeg commands (``-c:v libx264
-crf N -preset P``). This module rewrites the *video encoder* part of such a
command onto the verified MediaCodec hardware encoders, preserving every other
argument (inputs, maps, filters, audio, container flags) byte for byte.

Translation is deliberately conservative. A command is only translated when
the result keeps the same output contract (container, codec family, 8-bit
4:2:0, frame-accurate timeline); anything that would change output semantics
(lossless x264, 4:4:4 / 10-bit pixel formats, codecs without a hardware
encoder) stays on the CPU command, unmodified.
"""
from __future__ import annotations

import os
import re


# FFmpeg OPT_BOOL / argument-less options (fftools). Everything else takes
# exactly one value. Negated forms ("-noautorotate") are handled below.
_NOARG = {
    "y", "n", "stdin", "hide_banner", "stats", "copyts", "start_at_zero", "xerror",
    "benchmark", "benchmark_all", "debug_ts", "print_graphs", "dump", "hex",
    "auto_conversion_filters", "ignore_unknown", "copy_unknown", "recast_media",
    "report", "vstats", "qphist", "psnr", "bitexact", "find_stream_info",
    "seek_timestamp", "accurate_seek", "autorotate", "autoscale", "re", "copyinkf",
    "shortest", "an", "vn", "sn", "dn", "fix_sub_duration", "force_fps", "intra",
    "deinterlace", "sameq", "same_quant", "version", "buildconf", "formats",
    "muxers", "demuxers", "devices", "codecs", "decoders", "encoders", "bsfs",
    "protocols", "filters", "pix_fmts", "layouts", "sample_fmts", "dispositions",
    "colors", "hwaccels", "L", "isync", "apply_cropping_none",
}
_NEGATABLE = {
    "stdin", "stats", "copyts", "accurate_seek", "autorotate", "autoscale",
    "auto_conversion_filters", "find_stream_info", "seek_timestamp", "copyinkf",
}

_VCODEC_KEYS = ("-c:v", "-codec:v", "-vcodec", "-c:v:0", "-codec:v:0")
_ANY_CODEC_KEYS = ("-c", "-codec")

# x264/x265-only knobs that have no MediaCodec meaning and are dropped when
# the encoder is translated (rate control is re-expressed separately).
_X26X_ONLY = {
    "-preset", "-tune", "-x264-params", "-x264opts", "-x265-params", "-crf", "-qp",
    "-refs", "-coder", "-me_method", "-subq", "-trellis", "-aq-mode", "-aq-strength",
    "-psy", "-psy-rd", "-rc-lookahead", "-weightp", "-weightb", "-mbtree", "-fastfirstpass",
    "-bf", "-b_strategy", "-sc_threshold", "-keyint_min", "-nal-hrd", "-deblock",
    "-partitions", "-direct-pred", "-8x8dct", "-mixed-refs", "-cplxblur", "-qcomp",
    "-a53cc", "-forced-idr", "-crf_max", "-chromaoffset", "-udu_sei", "-x264-stats",
}

_VIDEO_EXT = {".mp4", ".m4v", ".mov", ".mkv", ".ts", ".mts", ".flv", ".3gp"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff", ".apng"}
_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff"}
# Containers whose *implicit* video encoder (no ``-c:v``) is H.264, verified
# against ``ffmpeg -h muxer=<m>``: mp4/mov/matroska/ipod default to h264, while
# mpegts defaults to mpeg2video, flv to flv1 and 3gp to h263 (m4v to mpeg4).
# Translating those to H.264 would change the codec family, so they stay CPU.
_DEFAULT_H264_FORMATS = {"mp4", "mov", "matroska", "mkv", "ipod"}

# ``-pix_fmt`` values the MediaCodec encoders reproduce losslessly (8-bit
# 4:2:0). ``None`` is deliberately *absent*: without an explicit ``-pix_fmt``
# the CPU encoder picks the format from the input and filter graph (10-bit
# sources stay 10-bit, 4:4:4 stays 4:4:4, some filters promote 4:2:0 to
# 4:4:4), so no explicit request means the output contract cannot be
# guaranteed on the hardware encoder.
_SUPPORTED_PIX = {"yuv420p", "yuvj420p", "nv12"}
_H264_PROFILES = {"baseline", "main", "high"}
_HEVC_PROFILES = {"main"}

# Muxers FFmpeg < 7 drives in constant-frame-rate mode by default.
_LEGACY_CFR_CONTAINERS = {"mp4", "mov", "m4v", "3gp", "3g2", "ipod", "ismv", "psp"}

HW_ENCODER_FOR = {"libx264": "h264", "h264": "h264", "libx265": "hevc", "hevc": "hevc"}

# Muxers that fan one output option out into several files (a segment list, a
# playlist plus its parts, or a tee fan-out). No single path owns them, so they
# are never translated and never staged as one destination.
_MULTIFILE_MUXERS = {
    "segment", "ssegment", "stream_segment", "hls", "dash", "tee", "webm_chunk",
}
# Muxers whose target is not the file they write (a stream endpoint, a discard,
# a frame buffer device): the dispatcher never stages or publishes them.
_NONFILE_MUXERS = {
    "null", "fifo", "rtsp", "rtmp", "rtmps", "udp", "tcp", "srt", "rist",
    "icecast", "sap", "caca", "fbdev", "opengl", "sdl", "alsa", "oss",
}
# Single-file muxers that carry no media stream to probe (text / checksums).
_TEXT_MUXERS = {"md5", "framemd5", "framecrc", "hash", "streamhash", "ffmetadata", "data"}

# A target like ``rtmp://host/app`` is a network endpoint, not a local file.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


class Segment:
    """Options followed by a target (an input URL or an output URL).

    ``start`` is the argv index of the segment's first option, ``end`` the
    index of its target. (Plain class: this module sits on the ffmpeg shim's
    hot path, where importing dataclasses costs measurable time under proot.)
    """
    __slots__ = ("opts", "target", "start", "end")

    def __init__(self, opts, target="", start=0, end=0):
        self.opts = opts
        self.target = target
        self.start = start
        self.end = end


class ParsedCommand:
    __slots__ = ("argv", "global_opts", "inputs", "outputs", "ok", "reason")

    def __init__(self, argv, global_opts, inputs, outputs, ok=True, reason=""):
        self.argv = argv
        self.global_opts = global_opts
        self.inputs = inputs
        self.outputs = outputs
        self.ok = ok
        self.reason = reason


def _takes_arg(opt: str) -> bool:
    name = opt[1:]
    if name in _NOARG:
        return False
    if name.startswith("no") and name[2:] in _NEGATABLE:
        return False
    return True


def parse(argv: list[str]) -> ParsedCommand:
    """Split ``argv`` (without the program name) into inputs and outputs."""
    pending: list[tuple[str, "str | None"]] = []
    pending_start = 0
    inputs: list[Segment] = []
    outputs: list[Segment] = []
    global_opts: list[tuple[str, "str | None"]] = []
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok == "-i":
            if i + 1 >= n:
                return ParsedCommand(argv, global_opts, inputs, outputs, False, "dangling -i")
            inputs.append(Segment(pending, argv[i + 1], pending_start, i + 1))
            pending = []
            i += 2
            pending_start = i
            continue
        if tok.startswith("-") and tok != "-" and len(tok) > 1:
            if _takes_arg(tok):
                if i + 1 >= n:
                    return ParsedCommand(argv, global_opts, inputs, outputs, False, f"dangling {tok}")
                pending.append((tok, argv[i + 1]))
                i += 2
            else:
                pending.append((tok, None))
                i += 1
            continue
        # A bare token is an output URL.
        outputs.append(Segment(pending, tok, pending_start, i))
        pending = []
        i += 1
        pending_start = i
    if pending:
        # Trailing options without an output, or global options only.
        global_opts.extend(pending)
    if not inputs and not outputs:
        return ParsedCommand(argv, global_opts, inputs, outputs, False, "no inputs/outputs")
    # Options placed before the first input that are global by nature are
    # still attached to that input; that is harmless for translation since
    # we only edit output segments.
    return ParsedCommand(argv, global_opts, inputs, outputs)


def _last(opts: list[tuple[str, "str | None"]], keys: tuple[str, ...]) -> "str | None":
    val = None
    for k, v in opts:
        if k in keys:
            val = v
    return val


def _has(opts: list[tuple[str, "str | None"]], key: str) -> bool:
    return any(k == key for k, _ in opts)


def is_pipe(url: str) -> bool:
    return url == "-" or url.startswith("pipe:") or url.startswith("fd:")


def _container(seg: Segment) -> "str | None":
    fmt = _last(seg.opts, ("-f",))
    if fmt:
        return fmt
    lower = seg.target.lower()
    for ext in _VIDEO_EXT | _IMAGE_EXT | _AUDIO_EXT | {".webm", ".avi", ".nut", ".y4m", ".mxf"}:
        if lower.endswith(ext):
            return ext[1:]
    return None


class OutputPlan:
    """Decision for one output: action is "gpu", "cpu" or "none"."""
    __slots__ = ("index", "target", "action", "reason", "hw_codec", "source_encoder", "crf")

    def __init__(self, index, target, action, reason="", hw_codec=None, source_encoder=None, crf=None):
        self.index = index
        self.target = target
        self.action = action
        self.reason = reason
        self.hw_codec = hw_codec
        self.source_encoder = source_encoder
        self.crf = crf

    def __repr__(self):
        return f"OutputPlan({self.index}, {self.target!r}, {self.action!r}, {self.reason!r})"


def _rate(text):
    try:
        if "/" in text:
            n, d = text.split("/", 1)
            return float(n) / float(d) if float(d) else None
        return float(text)
    except (TypeError, ValueError):
        return None


_FPS_FILTER = re.compile(r"(?:^|[,;\]\s])fps=(?:fps=)?([0-9.]+(?:/[0-9.]+)?)")
_LAVFI_RATE = re.compile(r"(?:^|[:=,])(?:rate|r)=([0-9.]+(?:/[0-9.]+)?)")


def output_fps(parsed, seg, probe_fps=None):
    """Best-effort output frame rate of ``seg``'s video, or None if unknown."""
    r = _last(seg.opts, ("-r", "-r:v"))
    if r:
        return _rate(r)
    graphs = [v for k, v in seg.opts if k in ("-vf", "-filter:v", "-filter:v:0")]
    graphs += [v for k, v in parsed.global_opts if k in ("-filter_complex", "-lavfi")]
    graphs += [v for s in parsed.inputs for k, v in s.opts if k in ("-filter_complex", "-lavfi")]
    for g in reversed(graphs):
        hits = _FPS_FILTER.findall(g or "")
        if hits:
            return _rate(hits[-1])
    if not parsed.inputs:
        return None
    first = parsed.inputs[0]
    r = _last(first.opts, ("-r", "-framerate"))
    if r:
        return _rate(r)
    if _last(first.opts, ("-f",)) == "lavfi":
        m = _LAVFI_RATE.findall(first.target)
        return _rate(m[-1]) if m else 25.0  # lavfi sources default to 25 fps
    if probe_fps is not None and not is_pipe(first.target):
        return probe_fps(first.target)
    return None


def _requested_gop(seg):
    gop = _last(seg.opts, ("-g", "-g:v"))
    params = _last(seg.opts, ("-x264-params", "-x264opts", "-x265-params")) or ""
    for part in re.split(r"[:,]", params):
        k, _, v = part.partition("=")
        if k.strip() == "keyint" and v.strip().isdigit():
            gop = v.strip()  # x264-params override -g in libx264
    try:
        return int(gop) if gop is not None else None
    except ValueError:
        return -1


def plan_output(seg: Segment, idx: int, parsed=None, probe_fps=None) -> OutputPlan:
    """Decide what to do with one output's video encode."""
    target = seg.target
    if _has(seg.opts, "-vn"):
        return OutputPlan(idx, target, "none", "no video stream (-vn)")
    container = (_container(seg) or "").lower()
    if container in {"null"}:
        return OutputPlan(idx, target, "none", "null muxer (analysis/decode only)")
    if container in _MULTIFILE_MUXERS:
        # One output option, several files: not translatable, and not ownable
        # as a single destination (the dispatcher refuses mixed commands).
        return OutputPlan(idx, target, "none", f"multi-file {container} muxer")
    if container in {e[1:] for e in _AUDIO_EXT} or container in {"wav", "mp3", "adts", "ogg", "flac", "opus"}:
        return OutputPlan(idx, target, "none", "audio-only container")
    if container in {e[1:] for e in _IMAGE_EXT} or container in {"image2", "image2pipe", "apng", "gif"} \
            or "%" in target:
        return OutputPlan(idx, target, "none", "image output")

    vcodec = _last(seg.opts, _VCODEC_KEYS) or _last(seg.opts, _ANY_CODEC_KEYS)
    if vcodec == "copy":
        return OutputPlan(idx, target, "none", "stream copy")
    if vcodec is None:
        if container in _DEFAULT_H264_FORMATS:
            vcodec = "libx264"  # FFmpeg's default video encoder for these muxers
        else:
            return OutputPlan(idx, target, "cpu", f"implicit encoder for container {container or '?'}")
    hw = HW_ENCODER_FOR.get(vcodec)
    if hw is None:
        return OutputPlan(idx, target, "cpu", f"no hardware encoder for {vcodec}")

    pix = _last(seg.opts, ("-pix_fmt", "-pix_fmt:v"))
    if pix is None:
        # No explicit pixel format: the CPU encoder would derive it from the
        # input and filter graph (10-bit / 4:4:4 preserved, 4:2:0 sometimes
        # promoted), and the hardware encoder always emits 8-bit 4:2:0.
        # Translation would silently change bit depth / chroma sampling.
        return OutputPlan(idx, target, "cpu",
                          "output pixel format not specified (hardware encoder would force 8-bit 4:2:0)")
    if pix not in _SUPPORTED_PIX:
        return OutputPlan(idx, target, "cpu", f"pixel format {pix} not supported by hardware encoder")
    crf_s = _last(seg.opts, ("-crf", "-crf:v", "-crf:v:0"))
    qp_s = _last(seg.opts, ("-qp", "-qp:v", "-qp:v:0"))
    params = (_last(seg.opts, ("-x264-params", "-x264opts", "-x265-params")) or "").lower()
    if (crf_s is not None and float(crf_s) == 0) or (qp_s is not None and float(qp_s) == 0) \
            or "lossless" in params or "qp=0" in params:
        return OutputPlan(idx, target, "cpu", "lossless encode requested")
    profile = _last(seg.opts, ("-profile:v", "-profile"))
    allowed = _H264_PROFILES if hw == "h264" else _HEVC_PROFILES
    if profile and profile.lower() not in allowed:
        return OutputPlan(idx, target, "cpu", f"profile {profile} not supported by hardware encoder")
    # Keyframe structure is part of the output contract (stream-copy cutting,
    # segmenting). The hardware encoder takes its I-frame interval in whole
    # seconds and ignores forced keyframes, so only exact cases are moved.
    if _has(seg.opts, "-force_key_frames") or _has(seg.opts, "-force_key_frames:v"):
        return OutputPlan(idx, target, "cpu", "forced keyframes are not supported by the hardware encoder path")
    gop = _requested_gop(seg)
    if gop is not None:
        if gop <= 0:
            return OutputPlan(idx, target, "cpu", f"unsupported GOP request {gop}")
        fps = output_fps(parsed, seg, probe_fps) if parsed is not None else None
        if not fps:
            return OutputPlan(idx, target, "cpu", f"GOP of {gop} frames requested but output frame rate unknown")
        secs = gop / fps
        if abs(secs - round(secs)) > 1e-6 or round(secs) < 1:
            return OutputPlan(idx, target, "cpu",
                              f"GOP of {gop} frames at {fps:g} fps is not a whole number of seconds "
                              "(hardware I-frame interval granularity)")
    crf = float(crf_s) if crf_s is not None else None
    return OutputPlan(idx, target, "gpu", "translated", hw, vcodec, crf)


def crf_to_cq(crf: float, codec: str) -> int:
    """Map an x264/x265 CRF to the MediaCodec CQ quality (0..100).

    Calibrated by VMAF against libx264 ``-preset medium`` on real content
    (gpu_native/bench/calibrate_cq.py); the table below is the CQ level whose
    VMAF matches or exceeds the CRF's VMAF. Between table points the mapping
    is linear; CRF outside the table is clamped.
    """
    table = CQ_TABLE.get(codec) or CQ_TABLE["h264"]
    pts = sorted(table.items())
    if crf <= pts[0][0]:
        return pts[0][1]
    if crf >= pts[-1][0]:
        return pts[-1][1]
    for (c0, q0), (c1, q1) in zip(pts, pts[1:]):
        if c0 <= crf <= c1:
            return int(round(q0 + (q1 - q0) * (crf - c0) / (c1 - c0)))
    return pts[-1][1]


# CRF -> CQ quality for the MTK hardware encoder (c2.mtk.avc.encoder).
#
# Calibrated against libx264 -preset medium with VMAF/SSIM on two real
# sources (1080p30 production render; 416x752 AI-generated clip), see
# gpu_native/bench/results/encoder_calibration.md. Rule: the CQ level whose
# VMAF is >= x264's on both sources, raised further where needed to keep the
# luma SSIM gap small. The hardware encoder has no B-frames, so equal quality
# costs ~1.1-2.2x x264-medium's bitrate; quality, not size, is held constant.
#   CRF18: x264 VMAF 96.25 / 99.00  ->  CQ98: 96.51 / 99.23
#   CRF23: x264 VMAF 93.65 / 97.04  ->  CQ90: 95.88 / 98.86
#   CRF28: x264 VMAF 89.29 / 91.56  ->  CQ75: ~93.0 / ~96.0 (CQ72: 92.12 / 94.96)
# HEVC uses the same table: at equal CQ the HEVC encoder is at least as good.
CQ_TABLE: dict = {
    "h264": {16.0: 100, 18.0: 98, 23.0: 90, 28.0: 75, 35.0: 55},
    "hevc": {16.0: 100, 18.0: 98, 23.0: 90, 28.0: 75, 35.0: 55},
}

DEFAULT_CRF = {"h264": 23.0, "hevc": 28.0}  # libx264 / libx265 defaults


def translate(argv: list[str], *, available: set[str], probe_fps=None,
              reference: str = "legacy") -> tuple["list[str] | None", list[OutputPlan]]:
    """Return (gpu_argv, plans). ``gpu_argv`` is None when nothing is translated.

    ``available`` is the set of verified hardware codecs (``{"h264", "hevc"}``).
    Only output segments are edited; inputs and globals are untouched.

    ``reference`` names the CPU FFmpeg being replaced, whose semantics the
    native 8.1 build must reproduce: "legacy" for FFmpeg < 7 (Debian 5.1),
    "modern" for >= 7 (e.g. Remotion's 7.1). FFmpeg < 7 differs in two
    output-contract ways that matter here:
      * ``-pix_fmt yuv420p`` converts to limited range (>= 7 keeps the
        source range) -> explicit ``-color_range tv``;
      * MOV/MP4 output defaults to constant frame rate, duplicating/dropping
        to fill the timeline (>= 7 passes variable timing through)
        -> explicit ``-fps_mode cfr``.
    """
    parsed = parse(argv)
    if not parsed.ok:
        return None, []
    plans = [plan_output(seg, i, parsed, probe_fps) for i, seg in enumerate(parsed.outputs)]
    for p in plans:
        if p.action == "gpu" and p.hw_codec not in available:
            p.action, p.reason = "cpu", f"hardware {p.hw_codec} encoder not verified on this device"
    if not any(p.action == "gpu" for p in plans):
        return None, plans

    out: list[str] = []
    cursor = 0
    for seg, plan in zip(parsed.outputs, plans):
        out.extend(argv[cursor:seg.start])
        if plan.action != "gpu":
            out.extend(argv[seg.start:seg.end + 1])
            cursor = seg.end + 1
            continue
        hw = plan.hw_codec
        new_opts: list[str] = []
        explicit_bitrate = None
        has_profile = False
        for k, v in seg.opts:
            if k in _VCODEC_KEYS or k.split(":")[0] in _X26X_ONLY:
                continue
            if k in _ANY_CODEC_KEYS and v in HW_ENCODER_FOR:
                continue  # replaced by the explicit -c:v below
            if k in ("-b:v", "-b:v:0", "-vb"):
                explicit_bitrate = v
            if k in ("-g", "-g:v"):
                continue  # re-emitted below from the effective GOP request
            if k in ("-profile:v", "-profile"):
                has_profile = True
            if k in ("-pix_fmt", "-pix_fmt:v"):
                if v == "yuvj420p":
                    # Full-range 4:2:0: same data, expressed as a range tag.
                    v = "yuv420p"
                    if not _has(seg.opts, "-color_range"):
                        new_opts.extend(["-color_range", "pc"])
                elif v in ("yuv420p", "nv12") and reference == "legacy" \
                        and not _has(seg.opts, "-color_range"):
                    new_opts.extend(["-color_range", "tv"])
            new_opts.append(k)
            if v is not None:
                new_opts.append(v)
        if reference == "legacy" and (_container(seg) or "").lower() in _LEGACY_CFR_CONTAINERS \
                and not any(_has(seg.opts, k) for k in ("-vsync", "-fps_mode", "-fps_mode:v")):
            new_opts.extend(["-fps_mode", "cfr"])
        enc = [f"-c:v", f"{hw}_mediacodec"]
        if explicit_bitrate:
            enc += ["-bitrate_mode", "vbr"]
        else:
            crf = plan.crf if plan.crf is not None else DEFAULT_CRF[hw]
            enc += ["-bitrate_mode", "cq", "-global_quality", str(crf_to_cq(crf, hw))]
        gop = _requested_gop(seg)
        # libx264's default keyint is 250; with the hardware encoder's
        # whole-second granularity this yields <= 250 frames at common rates.
        enc += ["-g", str(gop if gop else 250)]
        if hw == "h264" and not has_profile:
            enc += ["-profile:v", "high"]  # libx264's default for 8-bit 4:2:0
        # Encoder options go first so later user options (e.g. -tag:v) apply.
        out.extend(enc + new_opts)
        out.append(seg.target)
        cursor = seg.end + 1
    out.extend(argv[cursor:])
    return out, plans


def reads_stdin(argv: list[str]) -> bool:
    parsed = parse(argv)
    return any(is_pipe(s.target) for s in parsed.inputs)


def writes_stdout(argv: list[str]) -> bool:
    parsed = parse(argv)
    return any(is_pipe(s.target) for s in parsed.outputs)


# Filters that rasterise text through the system font stack (libass / freetype
# / fontconfig), plus the options and file types that imply one.  The leading
# guards keep unrelated tokens like ``bass=f=80`` from matching ``ass``.
_TEXT_FILTER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:drawtext|subtitles|ass|showtext)\b", re.IGNORECASE)
_FONT_OPTION_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:fontfile|fontsdir|font|textfile)=", re.IGNORECASE)
_FONT_FILE_RE = re.compile(r"\.(?:ttf|otf|ttc|pfb|pfa)\b", re.IGNORECASE)


def renders_text(argv: list[str]) -> bool:
    """True when the command rasterises text with the host font stack.

    Such commands depend on fontconfig resolving the same fonts as the Debian
    tools.  The PRoot-free worker runs in the Android host namespace, where
    ``/usr/share/fonts`` and Debian's ``/etc/fonts`` are invisible, so libass
    silently renders missing-glyph boxes while still exiting 0.  Callers keep
    these commands on the traced native path, which sees the Debian fonts.
    """
    for i, a in enumerate(argv):
        if not isinstance(a, str):
            continue
        if a.startswith("-") and a[1:].split(":", 1)[0] in (
                "vf", "filter", "filter_complex", "lavfi", "af", "filter_script", "filter_complex_script"):
            # Inspect the filter graph value itself (and any script path).
            value = argv[i + 1] if i + 1 < len(argv) and isinstance(argv[i + 1], str) else ""
            if _TEXT_FILTER_RE.search(value) or _FONT_OPTION_RE.search(value):
                return True
            if value and _FONT_FILE_RE.search(value):
                return True
        if _FONT_FILE_RE.search(a):
            return True
    return False


# ---------------------------------------------------------------------------
# Output ownership
#
# The dispatcher stages every *file* output of a command it accelerates,
# publishes them only after the attempt succeeded and its files validated, and
# rolls the whole group back together on failure. That contract only holds if
# the command's complete output surface is classified first: an output the
# dispatcher cannot own must make it refuse the command, never run an unowned
# writer (hardware *or* CPU fallback) against a production asset.
# ---------------------------------------------------------------------------

class OutputOwnership:
    """How the dispatcher must treat one output destination.

    ``kind`` is one of:
      ``"file"``   a single filesystem file: stage, validate, publish, rollback;
      ``"pipe"``   a pipe/fd stream: ordinary streaming semantics, never staged;
      ``"other"``  a discard or a non-file target (network/device): never staged;
      ``"unsafe"`` a *file-writing* destination that cannot be owned as one path
                   (a numbered pattern or a multi-file muxer): the command must
                   be refused rather than write it unprotected.

    ``media`` is the validation strength the transaction applies to the file
    (see ``ffmpeg_exec._validate``): ``"video"`` for an output produced by a
    video encode, ``"file"`` for a text/checksum muxer, ``"media"`` otherwise.
    """
    __slots__ = ("index", "target", "kind", "media", "reason")

    def __init__(self, index, target, kind, media="file", reason=""):
        self.index = index
        self.target = target
        self.kind = kind
        self.media = media
        self.reason = reason

    def __repr__(self):
        return f"OutputOwnership({self.index}, {self.target!r}, {self.kind!r}, {self.reason!r})"


def output_ownership(seg: Segment, idx: int, parsed=None) -> OutputOwnership:
    """Classify the destination of one output ``seg`` for the transaction."""
    target = seg.target
    if not target:
        return OutputOwnership(idx, target, "other", reason="output without a target")
    if is_pipe(target):
        return OutputOwnership(idx, target, "pipe", reason="pipe/fd stream")
    muxer = (_last(seg.opts, ("-f",)) or "").lower()
    if muxer in _NONFILE_MUXERS or _SCHEME_RE.match(target):
        return OutputOwnership(idx, target, "other", reason="non-file target")
    if "%" in target:
        # A numbered image/segment pattern: the set of files it writes is only
        # known at run time, so it cannot be staged or rolled back as one path.
        return OutputOwnership(idx, target, "unsafe", reason="numbered/pattern output")
    if muxer in _MULTIFILE_MUXERS:
        return OutputOwnership(idx, target, "unsafe", reason=f"multi-file {muxer} muxer")
    try:
        if os.path.exists(target) and not (os.path.isfile(target) or os.path.isdir(target)):
            # An existing device, FIFO or socket: not a file to own (and never
            # something to publish a temp over).
            return OutputOwnership(idx, target, "other", reason="existing non-regular target")
    except OSError:
        pass
    plan = plan_output(seg, idx, parsed)
    if muxer in _TEXT_MUXERS:
        media = "file"
    elif plan.action in ("gpu", "cpu"):
        media = "video"
    else:
        media = "media"
    return OutputOwnership(idx, target, "file", media, plan.reason)


def own_outputs(argv: list[str]) -> list[OutputOwnership]:
    """Classify every output of ``argv`` (the dispatcher's ownership surface)."""
    parsed = parse(argv)
    if not parsed.ok:
        return []
    return [output_ownership(seg, i, parsed) for i, seg in enumerate(parsed.outputs)]
