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
    "smoothstreaming", "hds",
}
# FFmpeg also *infers* the muxer from the output extension (av_guess_format), so
# ``playlist.m3u8`` is an HLS output without any ``-f hls``. These are the
# extensions of the multi-file muxers; the explicit and the implicit form must
# be classified identically (their segments/parts are written next to the
# playlist under names the command line never lists as an output).
_EXT_MUXER = {".m3u8": "hls", ".mpd": "dash", ".chk": "webm_chunk"}
# Output options that name an *additional* media file the muxer writes (segment
# parts, init segments, variant playlists, segment lists). Seeing one means the
# command writes files the dispatcher cannot stage as the single output path.
_SIDECAR_OPTS = {
    "hls_segment_filename", "hls_fmp4_init_filename", "master_pl_name", "var_stream_map",
    "segment_list", "init_seg_name", "media_seg_name", "single_file_name",
}
# Muxers whose target is not the file they write (a stream endpoint, a discard,
# a frame buffer device): the dispatcher never stages or publishes them.
_NONFILE_MUXERS = {
    "null", "fifo", "rtsp", "rtmp", "rtmps", "udp", "tcp", "srt", "rist",
    "icecast", "sap", "caca", "fbdev", "opengl", "sdl", "alsa", "oss",
}
# Single-file muxers that carry no media stream to probe (text / checksums).
_TEXT_MUXERS = {"md5", "framemd5", "framecrc", "hash", "streamhash", "ffmetadata", "data"}

# FFmpeg's ``url_find_protocol``: a URL scheme is the leading run of these
# characters when a ``:`` follows (no ``//`` is required: ``file:out.mp4``,
# ``tee:a.mp4|b.mp4`` and ``md5:sums`` are all scheme URLs).
_SCHEME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-.")
# Output protocols that write *local files* other than "the" target: a fan-out
# to several files (tee), a hash written into a file (md5) or a wrapper around
# another URL (crypto, ``crypto+file``). Their destinations cannot be owned.
_LOCAL_WRITING_SCHEMES = {"tee", "md5", "crypto"}


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


# ---------------------------------------------------------------------------
# URL / destination resolution
# ---------------------------------------------------------------------------

def url_scheme(target: str) -> "str | None":
    """The protocol FFmpeg would select for ``target`` (None means a plain path).

    Mirrors ``url_find_protocol``: the leading run of scheme characters when a
    ``:`` follows it. A name such as ``a:b.mp4`` is therefore the protocol
    ``a`` (FFmpeg rejects it), exactly as it is for the real command.
    """
    n = 0
    for ch in target:
        if ch not in _SCHEME_CHARS:
            break
        n += 1
    if 0 < n < len(target) and target[n] == ":":
        return target[:n]
    return None


def local_path(target: str) -> "str | None":
    """The filesystem path ``target`` opens, or None if it is not a local file.

    ``file:`` is FFmpeg's own prefix for a local file and is stripped exactly as
    its file protocol does: no percent-decoding and no special host part (so
    ``file:///tmp/a.mp4`` opens ``/tmp/a.mp4``, ``file://localhost/x`` opens
    ``/localhost/x`` and ``file:rel.mp4`` opens ``rel.mp4``). Pipes and every
    other protocol are not local files.
    """
    if not target or is_pipe(target):
        return None
    scheme = url_scheme(target)
    if scheme is None:
        return target
    if scheme == "file":
        path = target[len("file:"):]
        # ``file:///abs`` opens ``///abs``; on Linux a run of leading slashes
        # is the root directory, so name it that way.
        return "/" + path.lstrip("/") if path.startswith("//") else path
    return None


def write_target(path: str) -> str:
    """Where a write to ``path`` really lands: a symlink is followed.

    ffmpeg opens a symlinked output through the link, so its bytes land in the
    link's target. A staged replacement must be published over that target;
    ``rename`` over the link itself would replace the link and leave the asset
    it pointed to untouched.
    """
    try:
        if os.path.islink(path):
            return os.path.realpath(path)
    except OSError:
        pass
    return path


# ---------------------------------------------------------------------------
# Effective muxer / container
# ---------------------------------------------------------------------------

def effective_muxer(seg: Segment) -> str:
    """The muxer name that governs this output, lower-case, or "" if unknown.

    An explicit ``-f`` wins (the last one, as in FFmpeg). Otherwise the
    multi-file muxers that FFmpeg infers from the extension are recognised
    (``.m3u8`` -> hls, ``.mpd`` -> dash, ``.chk`` -> webm_chunk) so the implicit
    form is never mistaken for a single-file output.
    """
    fmt = _last(seg.opts, ("-f",))
    if fmt:
        return fmt.lower()
    path = local_path(seg.target) or seg.target
    return _EXT_MUXER.get(os.path.splitext(path.lower())[1], "")


def _container(seg: Segment) -> "str | None":
    muxer = effective_muxer(seg)
    if muxer:
        return muxer
    lower = (local_path(seg.target) or seg.target).lower()
    for ext in _VIDEO_EXT | _IMAGE_EXT | _AUDIO_EXT | {".webm", ".avi", ".nut", ".y4m", ".mxf"}:
        if lower.endswith(ext):
            return ext[1:]
    return None


# ---------------------------------------------------------------------------
# Option scoping
#
# FFmpeg resolves a per-stream option by walking the options *in command-line
# order* and keeping the last one whose stream specifier matches the stream, so
# ``-c copy -c:v libx265`` encodes video (and copies the rest) while
# ``-c:v libx265 -c copy`` copies everything. A specifier-less option applies
# to every stream, ``v``/``V`` to every video stream and ``v:N`` to the N-th
# one. Anything the translator cannot place with certainty (``0``, ``#id``,
# ``m:key``, ``disp:...``, ``v:1`` ...) is "ambiguous" and keeps the original
# CPU command.
# ---------------------------------------------------------------------------

_OPT_ALIASES = {
    "vcodec": ("c", "v"), "acodec": ("c", "a"), "scodec": ("c", "s"), "dcodec": ("c", "d"),
    "vb": ("b", "v"), "ab": ("b", "a"),
}
_TYPED = frozenset("asdt")  # audio, subtitle, data, attachment: never a video stream

# Options the planner reads or the translator rewrites: if any of them carries
# a specifier we cannot resolve, the output stays on the original CPU command.
_SCOPED_NAMES = (frozenset(o[1:] for o in _X26X_ONLY)
                 | {"c", "pix_fmt", "profile", "g", "b", "r", "force_key_frames", "pass", "passlogfile"})


def _canon(key: str) -> "tuple[str, str]":
    """Canonical ``(name, stream specifier)`` of an option key (``-vcodec`` -> ``c``/``v``)."""
    name, _, spec = key[1:].partition(":")
    if not spec and name in _OPT_ALIASES:
        return _OPT_ALIASES[name]
    if name == "codec":
        name = "c"
    return name, spec


def _video_scope(spec: str, single_video: bool) -> str:
    """"all" / "video" (applies to the video stream), "other" (never does) or "ambiguous"."""
    if spec == "":
        return "all"
    kind, _, rest = spec.partition(":")
    if kind in ("v", "V"):
        if rest == "" or (rest == "0" and single_video):
            return "video"
        return "ambiguous"
    if kind in _TYPED:
        return "other"
    return "ambiguous"


def _scoped_last(opts, name: str, single_video: bool) -> "tuple[int | None, str | None, bool]":
    """``(index, value, ambiguous)`` of the option ``name`` that wins for a video stream.

    The winner is the last option, in command-line order, whose specifier
    matches the video stream (specifier-less or video-typed). ``ambiguous`` is
    True when some option of that name has a specifier whose reach cannot be
    decided statically.
    """
    idx = val = None
    ambiguous = False
    for i, (k, v) in enumerate(opts):
        n, spec = _canon(k)
        if n != name:
            continue
        scope = _video_scope(spec, single_video)
        if scope in ("all", "video"):
            idx, val = i, v
        elif scope == "ambiguous":
            ambiguous = True
    return idx, val, ambiguous


def _has_unresolved_scope(opts, single_video: bool) -> "str | None":
    """Name of the first planner-relevant option with an undecidable stream specifier."""
    for k, _v in opts:
        n, spec = _canon(k)
        if n in _SCOPED_NAMES and _video_scope(spec, single_video) == "ambiguous":
            return k
    return None


def _single_video_stream(seg: Segment, parsed) -> bool:
    """True when the output can only contain one video stream.

    Without ``-map`` and without a complex filtergraph FFmpeg selects one
    stream per media type, so ``-c:v:0`` and ``-c:v`` name the same stream.
    """
    groups = [seg.opts]
    if parsed is not None:
        groups += [parsed.global_opts] + [s.opts for s in parsed.inputs] + [s.opts for s in parsed.outputs]
    for opts in groups:
        for k, _v in opts:
            if _canon(k)[0] in ("map", "filter_complex", "lavfi", "filter_complex_script"):
                return False
    return True


class OutputPlan:
    """Decision for one output: action is "gpu", "cpu" or "none".

    For a "gpu" plan ``codec_index`` is the position (in the output's option
    list) of the option whose value selects the video encoder, or None when the
    encoder is implicit; ``expect`` is the output contract the produced file is
    verified against before it is published (see ``ffmpeg_exec._validate``).
    """
    __slots__ = ("index", "target", "action", "reason", "hw_codec", "source_encoder", "crf",
                 "codec_index", "expect")

    def __init__(self, index, target, action, reason="", hw_codec=None, source_encoder=None, crf=None,
                 codec_index=None, expect=None):
        self.index = index
        self.target = target
        self.action = action
        self.reason = reason
        self.hw_codec = hw_codec
        self.source_encoder = source_encoder
        self.crf = crf
        self.codec_index = codec_index
        self.expect = expect

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
    r = _scoped_last(seg.opts, "r", _single_video_stream(seg, parsed))[1]
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


def _requested_gop(seg, single_video=True):
    gop = _scoped_last(seg.opts, "g", single_video)[1]
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
    if any(_canon(k)[0] in _SIDECAR_OPTS for k, _v in seg.opts):
        return OutputPlan(idx, target, "none", "output writes sidecar files (segments/lists)")
    if container in {e[1:] for e in _AUDIO_EXT} or container in {"wav", "mp3", "adts", "ogg", "flac", "opus"}:
        return OutputPlan(idx, target, "none", "audio-only container")
    if container in {e[1:] for e in _IMAGE_EXT} or container in {"image2", "image2pipe", "apng", "gif"} \
            or "%" in target:
        return OutputPlan(idx, target, "none", "image output")

    # The encoder is whatever FFmpeg would pick for the *video stream*: the
    # last codec option, in command-line order, whose specifier matches it.
    single = _single_video_stream(seg, parsed)
    unresolved = _has_unresolved_scope(seg.opts, single)
    if unresolved is not None:
        return OutputPlan(idx, target, "cpu", f"stream-specific option {unresolved} cannot be resolved statically")
    codec_index, vcodec, _amb = _scoped_last(seg.opts, "c", single)
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
    if _has(seg.opts, "-pass") or _has(seg.opts, "-passlogfile"):
        return OutputPlan(idx, target, "cpu", "two-pass encoding is not supported by the hardware encoder")

    pix = _scoped_last(seg.opts, "pix_fmt", single)[1]
    if pix is None:
        # No explicit pixel format: the CPU encoder would derive it from the
        # input and filter graph (10-bit / 4:4:4 preserved, 4:2:0 sometimes
        # promoted), and the hardware encoder always emits 8-bit 4:2:0.
        # Translation would silently change bit depth / chroma sampling.
        return OutputPlan(idx, target, "cpu",
                          "output pixel format not specified (hardware encoder would force 8-bit 4:2:0)")
    if pix not in _SUPPORTED_PIX:
        return OutputPlan(idx, target, "cpu", f"pixel format {pix} not supported by hardware encoder")
    crf_s = _scoped_last(seg.opts, "crf", single)[1]
    qp_s = _scoped_last(seg.opts, "qp", single)[1]
    params = (_last(seg.opts, ("-x264-params", "-x264opts", "-x265-params")) or "").lower()
    if (crf_s is not None and float(crf_s) == 0) or (qp_s is not None and float(qp_s) == 0) \
            or "lossless" in params or "qp=0" in params:
        return OutputPlan(idx, target, "cpu", "lossless encode requested")
    profile = _scoped_last(seg.opts, "profile", single)[1]
    allowed = _H264_PROFILES if hw == "h264" else _HEVC_PROFILES
    if profile and profile.lower() not in allowed:
        return OutputPlan(idx, target, "cpu", f"profile {profile} not supported by hardware encoder")
    # Keyframe structure is part of the output contract (stream-copy cutting,
    # segmenting). The hardware encoder takes its I-frame interval in whole
    # seconds and ignores forced keyframes, so only exact cases are moved.
    if any(_canon(k)[0] == "force_key_frames" for k, _v in seg.opts):
        return OutputPlan(idx, target, "cpu", "forced keyframes are not supported by the hardware encoder path")
    gop = _requested_gop(seg, single)
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
    # What the finished file must show before it may be published: the codec
    # family that was asked for, in 8-bit 4:2:0 (every pixel format the plan
    # accepts is a spelling of that). A command whose option order or scoping
    # the translation got wrong shows up here as a different codec / format.
    expect = {"codec": hw, "encoder": f"{hw}_mediacodec", "pix_fmts": ("yuv420p", "yuvj420p")}
    return OutputPlan(idx, target, "gpu", "translated", hw, vcodec, crf, codec_index, expect)


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
        single = _single_video_stream(seg, parsed)
        new_opts: list[str] = []
        explicit_bitrate = _scoped_last(seg.opts, "b", single)[1]
        pix_index = _scoped_last(seg.opts, "pix_fmt", single)[0]
        has_profile = _scoped_last(seg.opts, "profile", single)[0] is not None
        for i, (k, v) in enumerate(seg.opts):
            if i == plan.codec_index:
                # The option that selected the software encoder is rewritten
                # where it stands, keeping its key (and so its stream scope)
                # and its place in the command line. FFmpeg resolves codec
                # options by position, so moving it would let an earlier
                # ``-c copy`` (or a later one) change what it applies to.
                new_opts.extend([k, f"{hw}_mediacodec"])
                continue
            if k.split(":")[0] in _X26X_ONLY:
                continue
            name, spec = _canon(k)
            if name == "g" and _video_scope(spec, single) in ("all", "video"):
                continue  # re-emitted below from the effective GOP request
            if i == pix_index and k in ("-pix_fmt", "-pix_fmt:v", "-pix_fmt:v:0"):
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
        # An implicit encoder (no codec option reaches the video stream) is
        # named explicitly; an explicit one was rewritten in place above.
        enc = [] if plan.codec_index is not None else ["-c:v", f"{hw}_mediacodec"]
        if explicit_bitrate:
            enc += ["-bitrate_mode", "vbr"]
        else:
            crf = plan.crf if plan.crf is not None else DEFAULT_CRF[hw]
            enc += ["-bitrate_mode", "cq", "-global_quality", str(crf_to_cq(crf, hw))]
        gop = _requested_gop(seg, single)
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
                   (a numbered pattern, a multi-file muxer -- explicit or inferred
                   from the extension -- a sidecar-writing option, a protocol that
                   fans out to files, or a destination that aliases another output
                   or an input): the command must be refused rather than write it
                   unprotected.

    ``media`` is the validation strength the transaction applies to the file
    (see ``ffmpeg_exec._validate``): ``"video"`` for an output produced by a
    video encode, ``"file"`` for a text/checksum muxer, ``"media"`` otherwise.

    ``target`` is the token exactly as it appears on the command line (possibly
    a ``file:`` URL); ``path`` is the filesystem path that token opens, with a
    symlink followed to the file it really writes -- the path that is staged and
    published. It is set for ``"file"`` outputs only.
    """
    __slots__ = ("index", "target", "kind", "media", "reason", "path")

    def __init__(self, index, target, kind, media="file", reason="", path=None):
        self.index = index
        self.target = target
        self.kind = kind
        self.media = media
        self.reason = reason
        self.path = path

    def __repr__(self):
        return f"OutputOwnership({self.index}, {self.target!r}, {self.kind!r}, {self.reason!r})"


def output_ownership(seg: Segment, idx: int, parsed=None) -> OutputOwnership:
    """Classify the destination of one output ``seg`` for the transaction."""
    target = seg.target
    if not target:
        return OutputOwnership(idx, target, "other", reason="output without a target")
    if is_pipe(target):
        return OutputOwnership(idx, target, "pipe", reason="pipe/fd stream")
    muxer = effective_muxer(seg)
    scheme = url_scheme(target)
    if scheme is not None and scheme != "file":
        if scheme in _LOCAL_WRITING_SCHEMES or "+" in scheme:
            # tee fans out to several files, md5 writes a hash into a file and
            # crypto/``a+b`` wrap another URL: the files they write are not the
            # single path the command line shows.
            return OutputOwnership(idx, target, "unsafe", reason=f"{scheme}: protocol writes local files")
        return OutputOwnership(idx, target, "other", reason="non-file target")
    if muxer in _NONFILE_MUXERS:
        return OutputOwnership(idx, target, "other", reason="non-file target")
    path = local_path(target)
    if "%" in path:
        # A numbered image/segment pattern: the set of files it writes is only
        # known at run time, so it cannot be staged or rolled back as one path.
        return OutputOwnership(idx, target, "unsafe", reason="numbered/pattern output")
    if muxer in _MULTIFILE_MUXERS:
        return OutputOwnership(idx, target, "unsafe", reason=f"multi-file {muxer} muxer")
    if any(_canon(k)[0] in _SIDECAR_OPTS for k, _v in seg.opts):
        return OutputOwnership(idx, target, "unsafe", reason="output writes sidecar files (segments/lists)")
    try:
        if os.path.exists(path) and not (os.path.isfile(path) or os.path.isdir(path)):
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
    return OutputOwnership(idx, target, "file", media, plan.reason, write_target(path))


def own_outputs(argv: list[str]) -> list[OutputOwnership]:
    """Classify every output of ``argv`` (the dispatcher's ownership surface)."""
    parsed = parse(argv)
    if not parsed.ok:
        return []
    owned = [output_ownership(seg, i, parsed) for i, seg in enumerate(parsed.outputs)]
    # Two spellings of one destination (``a.mp4`` and ``file:a.mp4``, a symlink
    # and its target) must not become two independent staged writers, and an
    # output that is also an input is an in-place edit FFmpeg itself refuses:
    # staging it would turn that refusal into a silent rewrite.
    inputs = set()
    for seg in parsed.inputs:
        p = local_path(seg.target)
        if p:
            inputs.add(os.path.realpath(p))
    first: dict = {}
    for o in owned:
        if o.kind != "file":
            continue
        real = os.path.realpath(o.path)
        if real in inputs:
            o.kind, o.reason = "unsafe", "output is also an input (in-place edit)"
        elif real in first:
            o.kind, o.reason = "unsafe", f"same destination as output #{first[real].index}"
            first[real].kind = "unsafe"
            first[real].reason = f"same destination as output #{o.index}"
        else:
            first[real] = o
    return owned
