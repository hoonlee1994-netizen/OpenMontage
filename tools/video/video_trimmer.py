"""Video trimmer tool wrapping FFmpeg.

Provides cut, trim, speed adjustment, and concatenation of video segments.
All operations are deterministic and produce lossless or near-lossless output
by default.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ResumeSupport,
    ToolResult,
    ToolStability,
    ToolTier,
)


def probe_codec_types(path: Path) -> Optional[set[str]]:
    """Return the ffprobe `codec_type` set for a media file.

    Returns None when probing is unavailable or fails (ffprobe missing,
    unreadable file, invalid JSON) — callers must treat None as
    "unknown", never as "has no video".
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    streams = data.get("streams")
    if not isinstance(streams, list):
        return None
    return {
        str(s.get("codec_type"))
        for s in streams
        if isinstance(s, dict) and s.get("codec_type")
    }


# Copy-mode timing contract (B1 corrective repair v3).
#
# Stream-copy (`codec="copy"`) with input seeking (`-ss` before `-i`) starts
# from the preceding keyframe, so the produced window can be materially
# longer/shifted versus the requested [start, end) interval. The tool must
# not silently report such a window as success.
#
# Two gates enforce truthfulness:
#
# 1. Keyframe-alignment gate (pre-FFmpeg, video copy only): a nonzero start
#    must lie within KEYFRAME_ALIGN_TOLERANCE_SEC of an actual video
#    keyframe timestamp obtained via ffprobe (`-skip_frame nokey`). This
#    proves stream-copy can honor the requested start; otherwise the tool
#    fails closed and directs the caller to re-encode. start==0 is naturally
#    eligible and skips the probe.
#
# 2. Duration gate (post-output, explicit windows): the ffprobe format
#    duration of the output must satisfy
#      |actual - requested| <= allowed
#    where allowed = min(COPY_ABSOLUTE_CAP_SEC,
#                        requested * COPY_RELATIVE_TOLERANCE).
#    Re-encode mode is frame-accurate and is not subject to either gate.
#
# Tolerance rationale (observed on 30 fps H.264 fixtures outside the repo):
# - Keyframe-aligned dense-GOP (GOP=12, keyframe every 0.40 s) 3.0 s copies
#   overshoot by ~0.13-0.22 s from packet/container granularity (AAC frame
#   ~0.023 s, video frame ~0.033 s, MP4 muxing plus -avoid_negative_ts
#   make_zero). Sparse start=0 3.0 s copies overshoot ~0.13 s.
# - KEYFRAME_ALIGN_TOLERANCE_SEC=0.05 s covers ~1-2 video frames at 24-30 fps
#   plus timestamp quantization, while staying 8x below the dense GOP spacing
#   (0.40 s), so mid-GOP positions (e.g. 1.7 is 0.10 s from 1.6) are rejected.
# - COPY_ABSOLUTE_CAP_SEC=0.30 s sits just above the worst legitimate jitter
#   (~0.22 s) with margin, yet strictly below one dense GOP (0.40 s), so any
#   copy that drags in an extra GOP fails even if the keyframe gate missed.
# - COPY_RELATIVE_TOLERANCE=0.10 (10 %) keeps short windows strict: a 0.8 s
#   request allows only 0.08 s, so the observed 0.8->1.275 s case (delta
#   0.475 s, 59 %) and 0.8->1.034 s case (delta 0.234 s, 29 %) both fail and
#   require re-encode, while a 3.0 s request allows 0.30 s so the legitimate
#   ~0.17 s deviation passes. For long clips the absolute cap prevents the
#   tolerance from growing unbounded (10 % of 30 s would be 3 s of slack).
COPY_ABSOLUTE_CAP_SEC = 0.30
COPY_RELATIVE_TOLERANCE = 0.10
KEYFRAME_ALIGN_TOLERANCE_SEC = 0.05
# Deprecated fixed-threshold alias kept for import compatibility. Do NOT use
# for gating; the truthful gate is copy_allowed_delta().
COPY_DURATION_TOLERANCE_SEC = COPY_ABSOLUTE_CAP_SEC


def copy_allowed_delta(requested_duration: float) -> float:
    """Bounded proportional tolerance for copy-mode duration validation."""
    try:
        req = float(requested_duration)
    except (TypeError, ValueError):
        return COPY_ABSOLUTE_CAP_SEC
    if req <= 0:
        return 0.0
    return min(COPY_ABSOLUTE_CAP_SEC, req * COPY_RELATIVE_TOLERANCE)


def probe_output_duration(path: Path) -> Optional[float]:
    """Return the ffprobe format duration in seconds, or None if unknown."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    fmt = data.get("format")
    if not isinstance(fmt, dict):
        return None
    try:
        return float(fmt.get("duration"))
    except (TypeError, ValueError):
        return None


def probe_keyframe_times(path: Path) -> Optional[list[float]]:
    """Return sorted video keyframe timestamps (seconds) via ffprobe.

    Uses `-skip_frame nokey` so only keyframes are decoded, reading
    `best_effort_timestamp_time` which is populated on this ffprobe build
    (pkt_pts_time alone is empty here). Returns None when probing is
    unavailable or fails (ffprobe missing, unreadable file, invalid JSON,
    no video stream / no keyframes found) — callers must treat None as
    "unknown", never as "aligned" or "no keyframes".
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-select_streams", "v:0",
                "-skip_frame", "nokey",
                "-show_entries", "frame=best_effort_timestamp_time",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    frames = data.get("frames")
    if not isinstance(frames, list) or len(frames) == 0:
        return None
    times: list[float] = []
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        raw = fr.get("best_effort_timestamp_time")
        try:
            times.append(float(raw))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    if not times:
        return None
    return sorted(times)


def _try_remove_artifact(path: Path) -> tuple[bool, bool]:
    """Attempt to delete an invalid output artifact.

    Returns (removed, exists_after): removed is True only when unlink
    succeeded (or the file was already absent); exists_after reports
    whether the path still exists afterwards. Cleanup failure never
    converts media failure into success — callers must stay success=False.

    Side channel: on an OSError the exception type/message are recorded on
    ``_try_remove_artifact.last_error`` (else reset to None) so concat
    cleanup diagnostics can preserve the underlying exception text (O3)
    without changing this (removed, exists_after) contract relied upon by
    _cut. Never raises for OSError/FileNotFoundError.
    """
    try:
        _try_remove_artifact.last_error = None  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        path.unlink()
        removed = True
    except FileNotFoundError:
        removed = True
    except OSError as e:
        removed = False
        try:
            _try_remove_artifact.last_error = {  # type: ignore[attr-defined]
                "error_type": type(e).__name__,
                "error_message": str(e),
            }
        except Exception:
            pass
    try:
        exists_after = path.exists()
    except OSError:
        exists_after = True
    if not exists_after:
        removed = True
    else:
        removed = False
    return removed, exists_after


_try_remove_artifact.last_error = None  # type: ignore[attr-defined]


def _canon_path(p: Any) -> str:
    """Canonical identity string for temp-ownership comparisons.

    Never raises: falls back to progressively weaker normalizations.
    """
    try:
        return str(Path(p).resolve())
    except Exception:
        try:
            return str(Path(p).absolute())
        except Exception:
            return str(p)


def _last_remove_error() -> Optional[dict[str, str]]:
    """Return the last unlink diagnostic recorded by _try_remove_artifact.

    Returns None when the last removal succeeded, the path was trivially
    absent, or the remover in effect records nothing (e.g. a test double
    that replaced the module global). Mock-safe: never raises.
    """
    try:
        info = getattr(_try_remove_artifact, "last_error", None)
    except Exception:
        return None
    try:
        if isinstance(info, dict) and info.get("error_type"):
            return {
                "error_type": str(info.get("error_type")),
                "error_message": str(info.get("error_message", "")),
            }
    except Exception:
        return None
    return None


def _reset_remove_error() -> None:
    """Clear the _try_remove_artifact side channel before a removal attempt.

    Prevents a stale diagnostic from an earlier unrelated removal from
    being attributed to the current one. Harmless when the module global
    has been replaced by a test double (plain attribute assignment).
    Never raises.
    """
    try:
        _try_remove_artifact.last_error = None  # type: ignore[attr-defined]
    except Exception:
        pass


class VideoTrimmer(BaseTool):
    name = "video_trimmer"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "video_post"
    provider = "ffmpeg"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = ["cmd:ffmpeg"]
    install_instructions = (
        "Install FFmpeg: https://ffmpeg.org/download.html\n"
        "Windows: winget install FFmpeg\n"
        "macOS: brew install ffmpeg\n"
        "Linux: sudo apt install ffmpeg"
    )
    agent_skills = ["ffmpeg", "video-toolkit"]

    capabilities = ["cut", "trim", "speed_adjust", "concat"]

    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["cut", "speed", "concat"],
            },
            "input_path": {"type": "string"},
            "output_path": {"type": "string"},
            "start_seconds": {"type": "number", "minimum": 0},
            "end_seconds": {"type": "number", "minimum": 0},
            "speed_factor": {"type": "number", "minimum": 0.1, "maximum": 100.0},
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "input_path": {"type": "string"},
                        "start_seconds": {"type": "number"},
                        "end_seconds": {"type": "number"},
                    },
                },
            },
            "codec": {"type": "string", "default": "copy"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=2, ram_mb=1024, vram_mb=0, disk_mb=2000, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["FFmpeg error"])
    resume_support = ResumeSupport.FROM_START
    idempotency_key_fields = ["operation", "input_path", "start_seconds", "end_seconds", "speed_factor"]
    side_effects = ["writes video file to output_path"]
    user_visible_verification = ["Play trimmed output and verify cut points"]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        operation = inputs["operation"]
        start = time.time()

        try:
            if operation == "cut":
                result = self._cut(inputs)
            elif operation == "speed":
                result = self._speed(inputs)
            elif operation == "concat":
                result = self._concat(inputs)
            else:
                return ToolResult(success=False, error=f"Unknown operation: {operation}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        result.duration_seconds = round(time.time() - start, 2)
        return result

    def _cut(self, inputs: dict[str, Any]) -> ToolResult:
        input_path = Path(inputs["input_path"])
        if not input_path.exists():
            return ToolResult(success=False, error=f"Input not found: {input_path}")

        start_s = inputs.get("start_seconds", 0)
        end_s = inputs.get("end_seconds")
        codec = inputs.get("codec", "copy")
        output_path = Path(
            inputs.get("output_path", str(input_path.with_stem(f"{input_path.stem}_cut")))
        )

        # ---- 1. Interval validation BEFORE FFmpeg (no artifact) ----
        try:
            start_f = float(start_s)
        except (TypeError, ValueError):
            return ToolResult(
                success=False,
                error=(
                    "Invalid cut interval: start_seconds "
                    f"({start_s!r}) is not a number. No FFmpeg "
                    "operation was executed and no artifact was created."
                ),
                data={
                    "operation": "cut",
                    "input": str(input_path),
                    "output": str(output_path),
                    "start_seconds": start_s,
                    "end_seconds": end_s,
                    "codec": codec,
                },
            )
        if start_f < 0:
            return ToolResult(
                success=False,
                error=(
                    "Invalid cut interval: start_seconds "
                    f"({start_f}) must be >= 0. No FFmpeg operation "
                    "was executed and no artifact was created."
                ),
                data={
                    "operation": "cut",
                    "input": str(input_path),
                    "output": str(output_path),
                    "start_seconds": start_s,
                    "end_seconds": end_s,
                    "codec": codec,
                },
            )
        requested_duration: Optional[float] = None
        if end_s is not None:
            try:
                end_f = float(end_s)
            except (TypeError, ValueError):
                return ToolResult(
                    success=False,
                    error=(
                        "Invalid cut interval: end_seconds "
                        f"({end_s!r}) is not a number. No FFmpeg "
                        "operation was executed and no artifact was created."
                    ),
                    data={
                        "operation": "cut",
                        "input": str(input_path),
                        "output": str(output_path),
                        "start_seconds": start_s,
                        "end_seconds": end_s,
                        "codec": codec,
                    },
                )
            requested_duration = end_f - start_f
            if not (end_f > start_f):
                return ToolResult(
                    success=False,
                    error=(
                        "Invalid cut interval: end_seconds "
                        f"({end_f}) must be greater than start_seconds "
                        f"({start_f}); requested_duration={requested_duration}. "
                        "No FFmpeg operation was executed and no artifact "
                        "was created."
                    ),
                    data={
                        "operation": "cut",
                        "input": str(input_path),
                        "output": str(output_path),
                        "start_seconds": start_s,
                        "end_seconds": end_s,
                        "codec": codec,
                        "requested_duration": requested_duration,
                    },
                )

        # ---- 2. Input probe BEFORE FFmpeg: fail closed on unknown ----
        # Never infer audio-only from unknown. Unknown is distinct from
        # known_audio_only and can never support a media-preservation claim.
        # Never use filename extensions as proof of stream type.
        input_types = probe_codec_types(input_path)
        input_unknown = input_types is None or len(input_types) == 0
        input_known_video = not input_unknown and "video" in (input_types or set())
        input_known_audio_only = not input_unknown and "video" not in (input_types or set())
        input_probe = (
            "known_video" if input_known_video
            else "known_audio_only" if input_known_audio_only
            else "unknown"
        )
        if input_unknown:
            return ToolResult(
                success=False,
                error=(
                    "Cut input stream probe is unknown/inconclusive "
                    f"(input_streams={sorted(input_types) if input_types is not None else None}); "
                    "media preservation cannot be established, so success "
                    "is denied (fail-closed). This unknown-probe state is "
                    "distinct from positively identified audio-only input "
                    "and is never equivalent to audio-only. No FFmpeg "
                    "operation was executed and no artifact was created; "
                    "re-probe the input or retry once the source streams "
                    "are known."
                ),
                data={
                    "operation": "cut",
                    "input": str(input_path),
                    "output": str(output_path),
                    "start_seconds": start_s,
                    "end_seconds": end_s,
                    "codec": codec,
                    "requested_duration": requested_duration,
                    "input_streams": sorted(input_types) if input_types is not None else None,
                    "input_probe": "unknown",
                },
            )

        # ---- 3. Keyframe-alignment gate for video stream-copy ----
        # Only for positively identified VIDEO input with codec="copy".
        # start==0 is naturally eligible and skips the probe. A nonzero
        # start must lie within KEYFRAME_ALIGN_TOLERANCE_SEC of an actual
        # ffprobe keyframe timestamp; GOP assumptions, container type, and
        # filename are never used as proof. On misalignment (or unknown
        # keyframes) fail BEFORE the copy so no invalid artifact is made.
        keyframe_aligned: Optional[bool] = None
        nearest_kf: Optional[float] = None
        kf_distance: Optional[float] = None
        if input_known_video and codec == "copy" and start_f != 0:
            kf_times = probe_keyframe_times(input_path)
            if kf_times is None:
                return ToolResult(
                    success=False,
                    error=(
                        "copy-mode cut start cannot be proven keyframe-aligned "
                        f"(start_seconds={start_f}; keyframe probe unknown/failed). "
                        "Stream-copy starts from the preceding keyframe and is not "
                        "frame-accurate; exact arbitrary-position trimming requires "
                        "re-encode. Retry with codec='libx264' (re-encode) instead of "
                        "codec='copy'. No FFmpeg copy operation was executed and no "
                        "artifact was created."
                    ),
                    data={
                        "operation": "cut",
                        "input": str(input_path),
                        "output": str(output_path),
                        "start_seconds": start_s,
                        "end_seconds": end_s,
                        "codec": codec,
                        "requested_duration": requested_duration,
                        "input_streams": sorted(input_types or set()),
                        "input_probe": input_probe,
                        "keyframe_aligned": None,
                        "keyframe_tolerance_sec": KEYFRAME_ALIGN_TOLERANCE_SEC,
                    },
                )
            nearest_kf = min(kf_times, key=lambda t: abs(t - start_f))
            kf_distance = abs(nearest_kf - start_f)
            if kf_distance > KEYFRAME_ALIGN_TOLERANCE_SEC:
                return ToolResult(
                    success=False,
                    error=(
                        f"copy-mode cut start {start_f}s is not keyframe-aligned "
                        f"(nearest video keyframe at {nearest_kf:.6f}s, distance "
                        f"{kf_distance:.6f}s > tolerance "
                        f"{KEYFRAME_ALIGN_TOLERANCE_SEC:.3f}s). Stream-copy starts from "
                        "the preceding keyframe and is not frame-accurate; exact "
                        "arbitrary-position trimming requires re-encode. Retry with "
                        "codec='libx264' (re-encode) instead of codec='copy'. No "
                        "FFmpeg copy operation was executed and no artifact was "
                        "created; nothing was silently re-encoded."
                    ),
                    data={
                        "operation": "cut",
                        "input": str(input_path),
                        "output": str(output_path),
                        "start_seconds": start_s,
                        "end_seconds": end_s,
                        "codec": codec,
                        "requested_duration": requested_duration,
                        "input_streams": sorted(input_types or set()),
                        "input_probe": input_probe,
                        "keyframe_aligned": False,
                        "nearest_keyframe": nearest_kf,
                        "keyframe_distance": kf_distance,
                        "keyframe_tolerance_sec": KEYFRAME_ALIGN_TOLERANCE_SEC,
                        "keyframe_count": len(kf_times),
                    },
                )
            keyframe_aligned = True
        elif input_known_video and codec == "copy" and start_f == 0:
            keyframe_aligned = True
            nearest_kf = 0.0
            kf_distance = 0.0

        if codec == "copy":
            # Input seeking (-ss BEFORE -i): seeks to the nearest keyframe
            # and copies from there, so the video stream survives cuts at
            # non-keyframe positions. Output seeking (-ss after -i) with
            # `-c copy` can silently drop every video packet (B-frame
            # reordering / negative timestamps) and emit an audio-only MP4
            # while ffmpeg still exits 0. With input seeking, -to would be
            # misinterpreted against the seeked timeline, so express the
            # window as a duration (-t).
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_s),
                "-i", str(input_path),
            ]
            if end_s is not None:
                duration = float(end_s) - float(start_s)
                cmd.extend(["-t", str(duration)])
            cmd.extend(["-c", "copy", "-avoid_negative_ts", "make_zero"])
        else:
            # Re-encode is frame-accurate: output seeking (-ss after -i)
            # decodes and cuts exactly. Unchanged behavior.
            cmd = [
                "ffmpeg", "-y",
                "-i", str(input_path),
                "-ss", str(start_s),
            ]
            if end_s is not None:
                cmd.extend(["-to", str(end_s)])
            cmd.extend(["-c:v", codec, "-c:a", "aac"])
        cmd.append(str(output_path))

        self.run_command(cmd)

        # Post-cut validation (B1 fail-safe contract v3):
        # - success=True must mean the output is PROVEN to contain the
        #   required stream(s) for the positively identified input type.
        # - copy mode with an explicit window must additionally satisfy the
        #   bounded proportional duration gate.
        # Never use filename extensions as proof of stream type.
        allowed_tolerance: Optional[float] = None
        if codec == "copy" and requested_duration is not None:
            allowed_tolerance = copy_allowed_delta(requested_duration)
        base_data: dict[str, Any] = {
            "operation": "cut",
            "input": str(input_path),
            "output": str(output_path),
            "start_seconds": start_s,
            "end_seconds": end_s,
            "codec": codec,
            "requested_duration": requested_duration,
            "allowed_tolerance": allowed_tolerance,
            "copy_duration_tolerance_sec": allowed_tolerance if codec == "copy" else None,
            "copy_absolute_cap_sec": COPY_ABSOLUTE_CAP_SEC if codec == "copy" else None,
            "copy_relative_fraction": COPY_RELATIVE_TOLERANCE if codec == "copy" else None,
            "keyframe_aligned": keyframe_aligned,
            "keyframe_tolerance_sec": (
                KEYFRAME_ALIGN_TOLERANCE_SEC
                if (input_known_video and codec == "copy") else None
            ),
            "nearest_keyframe": nearest_kf,
            "keyframe_distance": kf_distance,
        }
        if not output_path.exists():
            return ToolResult(
                success=False,
                error=(
                    "FFmpeg exited 0 but the cut output is missing: "
                    f"{output_path}"
                ),
                data=base_data,
            )
        # Re-probe input for the record (unchanged file) and probe output.
        input_types_post = probe_codec_types(input_path)
        output_types = probe_codec_types(output_path)
        base_data["input_streams"] = (
            sorted(input_types_post) if input_types_post is not None else None
        )
        base_data["output_streams"] = (
            sorted(output_types) if output_types is not None else None
        )
        base_data["input_probe"] = input_probe
        if output_types is None:
            # Output probe inconclusive — do not claim a verified success,
            # but leave the file in place for manual inspection.
            base_data["output_probe"] = "unknown"
            return ToolResult(
                success=False,
                error=(
                    "Cut output could not be stream-validated (ffprobe "
                    "output probe unknown/unavailable or failed); not "
                    "claiming success. Output "
                    f"left at {output_path} for manual inspection."
                ),
                data=base_data,
            )
        base_data["output_probe"] = "known"
        if len(output_types) == 0:
            # Provably empty stream set — failure for every input type.
            # An empty set is never "audio-only" and never "video".
            removed, exists_after = _try_remove_artifact(output_path)
            base_data["cleanup_attempted"] = True
            base_data["cleanup_removed"] = removed
            base_data["output_exists_after_cleanup"] = exists_after
            cleanup_note = (
                "The invalid output was removed."
                if removed and not exists_after
                else (
                    f"Attempted removal of the invalid output failed "
                    f"(cleanup_removed={removed}, "
                    f"output_exists_after_cleanup={exists_after}); "
                    f"manual deletion of {output_path} may be required."
                )
            )
            if input_probe != "known_audio_only":
                empty_err = (
                    "Cut produced an output with an empty stream set "
                    f"(input_probe={input_probe}; output_streams=[]). An empty "
                    "stream set never counts as successful media preservation "
                    "(fail-closed). "
                    + cleanup_note
                    + " Retry with codec='libx264' (re-encode) once the source "
                    "streams are known."
                )
            else:
                empty_err = (
                    "Cut produced an output with an empty stream set "
                    f"(input_probe={input_probe}; output_streams=[]). An empty "
                    "stream set never counts as successful audio trimming "
                    "(fail-closed). "
                    + cleanup_note
                )
            return ToolResult(
                success=False,
                error=empty_err,
                data=base_data,
            )
        if input_known_video:
            if "video" not in output_types:
                # Copy mode dropped the video stream (typically a non-keyframe
                # cut) or re-encode lost video. Never report success; attempt
                # removal and report the outcome truthfully. Do NOT silently
                # fall back to a lossy re-encode of an explicitly requested
                # codec="copy" operation.
                removed, exists_after = _try_remove_artifact(output_path)
                base_data["cleanup_attempted"] = True
                base_data["cleanup_removed"] = removed
                base_data["output_exists_after_cleanup"] = exists_after
                cleanup_note = (
                    "The invalid output was removed."
                    if removed and not exists_after
                    else (
                        f"Attempted removal of the invalid output failed "
                        f"(cleanup_removed={removed}, "
                        f"output_exists_after_cleanup={exists_after}); "
                        f"manual deletion of {output_path} may be required."
                    )
                )
                if codec == "copy":
                    novideo_err = (
                        "copy-mode cut produced no video stream (input has "
                        f"video; output streams: {sorted(output_types)}). "
                        "Stream-copy cannot cut accurately at this position; "
                        "retry with codec='libx264' (re-encode) instead of "
                        "codec='copy'. " + cleanup_note
                    )
                else:
                    novideo_err = (
                        "cut produced no video stream (input has "
                        f"video; output streams: {sorted(output_types)}). "
                        "Video preservation cannot be established "
                        "(fail-closed). " + cleanup_note
                    )
                return ToolResult(
                    success=False,
                    error=novideo_err,
                    data=base_data,
                )
            # Output provably contains video. For copy mode with an explicit
            # window, additionally enforce the bounded proportional timing
            # contract.
            if codec == "copy" and requested_duration is not None:
                actual_duration = probe_output_duration(output_path)
                base_data["actual_duration"] = actual_duration
                if actual_duration is None:
                    # Cannot validate the cut window — fail closed, leave the
                    # file for manual inspection (timing unproven, streams OK).
                    return ToolResult(
                        success=False,
                        error=(
                            "copy-mode cut output has video but its duration "
                            "could not be validated (ffprobe duration probe "
                            "unknown/failed); not claiming the requested "
                            f"[{start_s}, {end_s}] window. Output left at "
                            f"{output_path} for manual inspection."
                        ),
                        data=base_data,
                    )
                delta = abs(actual_duration - requested_duration)
                base_data["duration_delta"] = delta
                assert allowed_tolerance is not None
                if delta > allowed_tolerance:
                    removed, exists_after = _try_remove_artifact(output_path)
                    base_data["cleanup_attempted"] = True
                    base_data["cleanup_removed"] = removed
                    base_data["output_exists_after_cleanup"] = exists_after
                    cleanup_note = (
                        "The invalid output was removed."
                        if removed and not exists_after
                        else (
                            f"Attempted removal of the invalid output failed "
                            f"(cleanup_removed={removed}, "
                            f"output_exists_after_cleanup={exists_after}); "
                            f"manual deletion of {output_path} may be required."
                        )
                    )
                    return ToolResult(
                        success=False,
                        error=(
                            "copy-mode cut cannot satisfy the requested "
                            f"[{start_s}, {end_s}] window within tolerance "
                            f"(requested {requested_duration:.3f}s, actual "
                            f"{actual_duration:.3f}s, delta {delta:.3f}s > "
                            f"allowed {allowed_tolerance:.3f}s "
                            f"[min(cap {COPY_ABSOLUTE_CAP_SEC:.3f}s, "
                            f"requested*frac {requested_duration:.3f}s*"
                            f"{COPY_RELATIVE_TOLERANCE:.2f}= "
                            f"{requested_duration * COPY_RELATIVE_TOLERANCE:.3f}s)]). "
                            "Stream-copy starts from the preceding keyframe and "
                            "is not frame-accurate; retry with codec='libx264' "
                            "(re-encode) instead of codec='copy'. " + cleanup_note
                        ),
                        data=base_data,
                    )
            else:
                base_data["actual_duration"] = probe_output_duration(output_path)

            base_data["output_has_video"] = "video" in output_types
            base_data["output_has_audio"] = "audio" in output_types
            return ToolResult(
                success=True,
                data=base_data,
                artifacts=[str(output_path)],
            )
        # Positively identified audio-only input.
        if "audio" not in output_types:
            removed, exists_after = _try_remove_artifact(output_path)
            base_data["cleanup_attempted"] = True
            base_data["cleanup_removed"] = removed
            base_data["output_exists_after_cleanup"] = exists_after
            cleanup_note = (
                "The invalid output was removed."
                if removed and not exists_after
                else (
                    f"Attempted removal of the invalid output failed "
                    f"(cleanup_removed={removed}, "
                    f"output_exists_after_cleanup={exists_after}); "
                    f"manual deletion of {output_path} may be required."
                )
            )
            return ToolResult(
                success=False,
                error=(
                    "audio-only cut produced output without an audio stream "
                    f"(input streams: {sorted(input_types or set())}; output "
                    f"streams: {sorted(output_types)}). Audio preservation "
                    "cannot be established (fail-closed). " + cleanup_note
                ),
                data=base_data,
            )
        # Output provably contains audio. For copy mode with an explicit
        # window, enforce the same bounded proportional timing contract.
        if codec == "copy" and requested_duration is not None:
            actual_duration = probe_output_duration(output_path)
            base_data["actual_duration"] = actual_duration
            if actual_duration is None:
                return ToolResult(
                    success=False,
                    error=(
                        "audio-only copy-mode cut output has audio but its "
                        "duration could not be validated (ffprobe duration "
                        "probe unknown/failed); not claiming the requested "
                        f"[{start_s}, {end_s}] window. Output left at "
                        f"{output_path} for manual inspection."
                    ),
                    data=base_data,
                )
            delta = abs(actual_duration - requested_duration)
            base_data["duration_delta"] = delta
            assert allowed_tolerance is not None
            if delta > allowed_tolerance:
                removed, exists_after = _try_remove_artifact(output_path)
                base_data["cleanup_attempted"] = True
                base_data["cleanup_removed"] = removed
                base_data["output_exists_after_cleanup"] = exists_after
                cleanup_note = (
                    "The invalid output was removed."
                    if removed and not exists_after
                    else (
                        f"Attempted removal of the invalid output failed "
                        f"(cleanup_removed={removed}, "
                        f"output_exists_after_cleanup={exists_after}); "
                        f"manual deletion of {output_path} may be required."
                    )
                )
                return ToolResult(
                    success=False,
                    error=(
                        "audio-only copy-mode cut cannot satisfy the requested "
                        f"[{start_s}, {end_s}] window within tolerance "
                        f"(requested {requested_duration:.3f}s, actual "
                        f"{actual_duration:.3f}s, delta {delta:.3f}s > "
                        f"allowed {allowed_tolerance:.3f}s). Retry with a "
                        "re-encode codec instead of codec='copy'. " + cleanup_note
                    ),
                    data=base_data,
                )
        else:
            base_data["actual_duration"] = probe_output_duration(output_path)
        base_data["output_has_video"] = "video" in output_types
        base_data["output_has_audio"] = "audio" in output_types
        return ToolResult(
            success=True,
            data=base_data,
            artifacts=[str(output_path)],
        )

    def _speed(self, inputs: dict[str, Any]) -> ToolResult:
        input_path = Path(inputs["input_path"])
        if not input_path.exists():
            return ToolResult(success=False, error=f"Input not found: {input_path}")

        factor = inputs.get("speed_factor", 1.0)
        output_path = Path(
            inputs.get("output_path", str(input_path.with_stem(f"{input_path.stem}_speed")))
        )

        # Video: setpts adjusts presentation timestamps (inverse of speed)
        # Audio: atempo adjusts audio speed (must chain for >2x)
        video_filter = f"setpts={1.0/factor}*PTS"
        audio_filters = self._build_atempo_chain(factor)

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-filter:v", video_filter,
            "-filter:a", audio_filters,
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "aac",
            str(output_path),
        ]

        self.run_command(cmd)

        return ToolResult(
            success=True,
            data={
                "operation": "speed",
                "input": str(input_path),
                "output": str(output_path),
                "speed_factor": factor,
            },
            artifacts=[str(output_path)],
        )

    def _concat(self, inputs: dict[str, Any]) -> ToolResult:
        raw_segments = inputs.get("segments", [])
        if isinstance(raw_segments, (list, tuple)):
            segments: list[Any] = list(raw_segments)
        elif not raw_segments:
            return ToolResult(success=False, error="No segments provided for concat")
        else:
            return ToolResult(
                success=False,
                error=(
                    "Invalid concat segments: expected a list of segment "
                    f"objects, got {type(raw_segments).__name__}. "
                    "No final artifact was created."
                ),
            )
        if not segments:
            return ToolResult(success=False, error="No segments provided for concat")

        output_path = Path(inputs.get("output_path", "concat_output.mp4"))

        # Codec finding (documented before behavior change): the shared
        # input_schema advertises top-level "codec" (default "copy"), but
        # _concat historically ignored it and hardcoded `-c copy` for
        # trimmed segments. Trimmed segments now honor it by delegating to
        # _cut (default "copy" preserves the previous stream-copy meaning;
        # an explicit caller-supplied codec such as "libx264" yields a
        # frame-accurate re-encode trim under the same _cut contract — this
        # is caller-requested, never a silent fallback). Untrimmed segments
        # are passed through untouched regardless of codec.
        codec = inputs.get("codec", "copy")

        # temp ownership (O1): ONLY paths explicitly reserved by this
        # invocation (owned_temp_paths) plus the concat list actually
        # created here (list_path_used, when list_created_by_us) may be
        # deleted by cleanup. concat_inputs holds the ordered entries for
        # the concat list (reserved temps for trimmed segments, original
        # paths for untrimmed segments — originals must never be deleted).
        # original_keys tracks resolved original inputs so they can never
        # enter the ownership set, even on filename collision.
        owned_temp_paths: list[Path] = []
        owned_temp_keys: set[str] = set()
        original_keys: set[str] = set()
        list_path_used: Optional[Path] = None
        list_created_by_us = False
        concat_inputs: list[Path] = []
        temp_dir = output_path.parent / ".concat_tmp"

        # O4: snapshot the pre-existing final output before touching
        # anything. Only an invocation-created partial may be a removal
        # candidate; a pre-existing user file is never deleted here.
        try:
            output_existed_before = output_path.exists()
        except Exception:
            output_existed_before = True  # conservative: never delete on doubt

        def _is_original(cand: Path) -> bool:
            try:
                return _canon_path(cand) in original_keys
            except Exception:
                return False

        def _is_owned(cand: Path) -> bool:
            try:
                return _canon_path(cand) in owned_temp_keys
            except Exception:
                return False

        def _register_owned(p: Path) -> None:
            # Original inputs must never enter the ownership set, even on
            # collision — the caller must pick a different temp name.
            try:
                if _canon_path(p) in original_keys:
                    return
                key = _canon_path(p)
                if key not in owned_temp_keys:
                    owned_temp_keys.add(key)
                    owned_temp_paths.append(p)
            except Exception:
                pass

        def _ensure_temp_dir() -> None:
            # Lazily created (temp-dir lifecycle): no .concat_tmp exists
            # until a temp segment or the concat list is actually required,
            # so early exits (empty/malformed segments) cannot leak it.
            temp_dir.mkdir(parents=True, exist_ok=True)

        def _reserve_temp_path(index: int, suffix: str) -> Path:
            # Collision-safe reservation (O1): the default
            # seg_{index:04d}{suffix} preserves the historical shape; on
            # collision with an on-disk file, an original input, or the
            # final output path, fall back deterministically to a _cNN
            # suffixed name. The original is preserved byte-for-byte and
            # never registered as owned.
            _ensure_temp_dir()
            base = f"seg_{index:04d}"
            candidate = temp_dir / f"{base}{suffix}"
            counter = 0
            while True:
                try:
                    on_disk = candidate.exists()
                except Exception:
                    on_disk = True  # unknown state counts as collision
                try:
                    collides_output = _canon_path(candidate) == _canon_path(
                        output_path
                    )
                except Exception:
                    collides_output = True
                if (
                    (not on_disk)
                    and (not _is_original(candidate))
                    and (not collides_output)
                ):
                    break
                counter += 1
                if counter > 999:
                    raise RuntimeError(
                        f"cannot reserve collision-safe temp path "
                        f"for segment {index}"
                    )
                candidate = temp_dir / f"{base}_c{counter:02d}{suffix}"
            _register_owned(candidate)
            return candidate

        def _cleanup_temps() -> dict[str, Any]:
            """Remove ONLY explicitly owned temp files (O1).

            Best-effort and non-throwing: never raises to the main control
            flow, so a cleanup failure can never mask the primary trim/join
            error. Deletion requires membership in the owned-temp
            collection (or being the concat list created by this
            invocation) — directory membership alone never proves
            ownership, so original inputs are never touched. Reports
            truthfully: per-file removal outcome, which paths remain, and
            every cleanup failure with its exception type/text (O3) in
            ``cleanup_error``, kept separate from the primary failure.
            """
            try:
                file_details: list[dict[str, Any]] = []
                problems: list[str] = []
                try:
                    snapshot = list(owned_temp_paths)
                except Exception as e:
                    snapshot = []
                    problems.append(
                        f"owned-temp snapshot failed: {type(e).__name__}: {e}"
                    )
                for tf in snapshot:
                    try:
                        try:
                            if not _is_owned(tf):
                                continue  # not ours; never delete
                        except Exception:
                            continue  # cannot prove ownership; never delete
                        _reset_remove_error()
                        try:
                            removed, exists_after = _try_remove_artifact(tf)
                        except Exception as e:
                            try:
                                file_details.append(
                                    {
                                        "path": str(tf),
                                        "operation": "unlink",
                                        "removed": False,
                                        "exists_after": True,
                                        "error_type": type(e).__name__,
                                        "error_message": str(e),
                                        "cleanup_error": f"{type(e).__name__}: {e}",
                                    }
                                )
                            except Exception:
                                pass
                            problems.append(
                                f"{tf}: unlink raised "
                                f"{type(e).__name__}: {e}"
                            )
                            continue
                        try:
                            detail: dict[str, Any] = {
                                "path": str(tf),
                                "operation": "unlink",
                                "removed": bool(removed),
                                "exists_after": bool(exists_after),
                            }
                            if (not removed) or exists_after:
                                diag = _last_remove_error()
                                if diag is not None:
                                    detail["error_type"] = diag["error_type"]
                                    detail["error_message"] = diag[
                                        "error_message"
                                    ]
                                    detail["cleanup_error"] = (
                                        f"{diag['error_type']}: "
                                        f"{diag['error_message']}"
                                    )
                                    problems.append(
                                        f"{tf}: unlink failed: "
                                        f"{diag['error_type']}: "
                                        f"{diag['error_message']}"
                                    )
                                else:
                                    problems.append(
                                        f"{tf}: unlink reported "
                                        f"removed={removed} "
                                        f"exists_after={exists_after}"
                                    )
                            file_details.append(detail)
                        except Exception:
                            pass
                    except Exception as e:
                        try:
                            file_details.append(
                                {
                                    "path": str(tf),
                                    "operation": "unlink",
                                    "removed": False,
                                    "exists_after": True,
                                    "error_type": type(e).__name__,
                                    "error_message": str(e),
                                    "cleanup_error": f"{type(e).__name__}: {e}",
                                }
                            )
                        except Exception:
                            pass
                        problems.append(
                            f"temp cleanup failed: {type(e).__name__}: {e}"
                        )
                list_removed: Optional[bool] = None
                list_exists_after = False
                # Only the concat list actually created by this invocation
                # is cleanup-owned; a pre-existing foreign file is never
                # touched (O1).
                if list_created_by_us and list_path_used is not None:
                    try:
                        _reset_remove_error()
                        try:
                            list_removed, list_exists_after = _try_remove_artifact(
                                list_path_used
                            )
                        except Exception as e:
                            list_removed, list_exists_after = False, True
                            problems.append(
                                f"{list_path_used}: unlink raised "
                                f"{type(e).__name__}: {e}"
                            )
                        else:
                            if (not list_removed) or list_exists_after:
                                diag = _last_remove_error()
                                if diag is not None:
                                    problems.append(
                                        f"{list_path_used}: unlink failed: "
                                        f"{diag['error_type']}: "
                                        f"{diag['error_message']}"
                                    )
                                else:
                                    problems.append(
                                        f"{list_path_used}: unlink reported "
                                        f"removed={list_removed} "
                                        f"exists_after={list_exists_after}"
                                    )
                    except Exception as e:
                        problems.append(
                            f"concat_list cleanup failed: "
                            f"{type(e).__name__}: {e}"
                        )
                        list_removed = None
                        list_exists_after = True
                try:
                    try:
                        temp_dir.rmdir()
                    except FileNotFoundError:
                        pass
                    except OSError as e:
                        problems.append(
                            f"{temp_dir}: rmdir failed: "
                            f"{type(e).__name__}: {e}"
                        )
                    except Exception as e:
                        problems.append(
                            f"{temp_dir}: rmdir failed: "
                            f"{type(e).__name__}: {e}"
                        )
                except Exception as e:
                    problems.append(
                        f"temp_dir removal failed: {type(e).__name__}: {e}"
                    )
                try:
                    temp_dir_exists_after = temp_dir.exists()
                except Exception:
                    temp_dir_exists_after = True
                try:
                    remaining: list[str] = [
                        d["path"]
                        for d in file_details
                        if isinstance(d, dict) and d.get("exists_after")
                    ]
                    if list_exists_after and list_path_used is not None:
                        try:
                            remaining.append(str(list_path_used))
                        except Exception:
                            pass
                    if temp_dir_exists_after:
                        try:
                            remaining.append(str(temp_dir))
                        except Exception:
                            pass
                except Exception:
                    remaining = []
                try:
                    cleanup_error: Optional[str] = (
                        "; ".join(problems) if problems else None
                    )
                    cleanup_ok = (len(remaining) == 0) and (cleanup_error is None)
                except Exception:
                    try:
                        cleanup_error = (
                            "; ".join(problems) if problems else "cleanup unknown"
                        )
                    except Exception:
                        cleanup_error = "cleanup unknown"
                    cleanup_ok = False
                return {
                    "cleanup_attempted": True,
                    "temp_file_details": file_details,
                    "concat_list_removed": list_removed,
                    "concat_list_exists_after": list_exists_after,
                    "temp_dir_exists_after": temp_dir_exists_after,
                    "cleanup_error": cleanup_error,
                    "cleanup_remaining_paths": remaining,
                    "cleanup_ok": cleanup_ok,
                }
            except Exception as e:
                try:
                    td_exists = temp_dir.exists()
                except Exception:
                    td_exists = True
                try:
                    td_str = str(temp_dir)
                except Exception:
                    td_str = ".concat_tmp"
                return {
                    "cleanup_attempted": True,
                    "temp_file_details": [],
                    "concat_list_removed": None,
                    "concat_list_exists_after": True,
                    "temp_dir_exists_after": td_exists,
                    "cleanup_error": (
                        f"cleanup helper failed: {type(e).__name__}: {e}"
                    ),
                    "cleanup_remaining_paths": [td_str],
                    "cleanup_ok": False,
                }

        def _safe_cleanup() -> dict[str, Any]:
            """Invoke _cleanup_temps without ever raising (defense in depth)."""
            try:
                return _cleanup_temps()
            except Exception as e:
                try:
                    td_exists = temp_dir.exists()
                except Exception:
                    td_exists = True
                try:
                    td_str = str(temp_dir)
                except Exception:
                    td_str = ".concat_tmp"
                return {
                    "cleanup_attempted": True,
                    "temp_file_details": [],
                    "concat_list_removed": None,
                    "concat_list_exists_after": True,
                    "temp_dir_exists_after": td_exists,
                    "cleanup_error": (
                        f"cleanup helper raised: {type(e).__name__}: {e}"
                    ),
                    "cleanup_remaining_paths": [td_str],
                    "cleanup_ok": False,
                }

        for i, seg in enumerate(segments):
            # ---- Malformed-segment guard (O2): every segment is validated
            # inside concat failure authority before any unsafe
            # dereference. A malformed segment returns structured failure
            # (index, reason, cleanup state); KeyError/TypeError can never
            # bypass it and the final join never executes.
            malformed_reason: Optional[str] = None
            seg_input: Optional[Path] = None
            if not isinstance(seg, dict):
                malformed_reason = (
                    f"malformed concat segment {i}: expected an "
                    f"object/mapping with 'input_path', got "
                    f"{type(seg).__name__}"
                )
            elif "input_path" not in seg:
                malformed_reason = (
                    f"malformed concat segment {i}: missing required "
                    "'input_path'"
                )
            else:
                raw_seg_input = seg["input_path"]
                if isinstance(raw_seg_input, str) and raw_seg_input == "":
                    malformed_reason = (
                        f"malformed concat segment {i}: 'input_path' is empty"
                    )
                else:
                    try:
                        candidate_input = Path(raw_seg_input)
                    except Exception as e:
                        candidate_input = None  # type: ignore[assignment]
                        malformed_reason = (
                            f"malformed concat segment {i}: unusable "
                            f"'input_path' ({raw_seg_input!r}): "
                            f"{type(e).__name__}: {e}"
                        )
                    if malformed_reason is None:
                        if str(candidate_input) in ("", "."):
                            malformed_reason = (
                                f"malformed concat segment {i}: unusable "
                                f"'input_path' ({raw_seg_input!r})"
                            )
                        else:
                            seg_input = candidate_input
            if malformed_reason is not None:
                cleanup = _safe_cleanup()
                return ToolResult(
                    success=False,
                    error=(
                        f"{malformed_reason}; concat aborted before final "
                        f"join ({len(segments)} segments requested). "
                        "No final artifact was created."
                    ),
                    data={
                        "operation": "concat",
                        "segment_count": len(segments),
                        "failed_segment_index": i,
                        "segment_input": None,
                        "segment_error": malformed_reason,
                        "malformed_segment": True,
                        "final_output_created": False,
                        **cleanup,
                    },
                )
            assert seg_input is not None
            # Track the original so it can never enter the owned-temp set
            # and temp reservation can avoid colliding with it (O1).
            try:
                original_keys.add(_canon_path(seg_input))
            except Exception:
                pass
            try:
                seg_exists = seg_input.exists()
            except Exception:
                seg_exists = False
            if not seg_exists:
                cleanup = _safe_cleanup()
                return ToolResult(
                    success=False,
                    error=(
                        f"concat segment {i} input not found: {seg_input}; "
                        f"concat aborted before final join "
                        f"({len(segments)} segments requested). "
                        "No final artifact was created."
                    ),
                    data={
                        "operation": "concat",
                        "segment_count": len(segments),
                        "failed_segment_index": i,
                        "segment_input": str(seg_input),
                        "final_output_created": False,
                        **cleanup,
                    },
                )

            seg_start = seg.get("start_seconds")
            seg_end = seg.get("end_seconds")

            if seg_start is not None or seg_end is not None:
                # Single trimming authority: delegate to the hardened _cut
                # contract (interval validation, fail-closed probe, video
                # keyframe-alignment gate, bounded copy-duration semantics,
                # required output streams, truthful cleanup). No independent
                # stream-copy path is maintained here; a failed trim must
                # never enter the concat list.
                # Explicit ownership (O1): reserve a collision-safe temp
                # path owned by this invocation BEFORE invoking _cut, so a
                # failed trim that intentionally leaves its artifact for
                # inspection is still cleanup-owned. The original input is
                # preserved byte-for-byte and never registered as owned.
                try:
                    temp_path = _reserve_temp_path(i, seg_input.suffix)
                except Exception as e:
                    cleanup = _safe_cleanup()
                    exc_text = f"{type(e).__name__}: {e}"
                    return ToolResult(
                        success=False,
                        error=(
                            f"concat segment {i} temp reservation raised "
                            f"{exc_text}; concat aborted before final join "
                            f"({len(segments)} segments requested). "
                            "No final artifact was created."
                        ),
                        data={
                            "operation": "concat",
                            "segment_count": len(segments),
                            "failed_segment_index": i,
                            "segment_input": str(seg_input),
                            "segment_start_seconds": seg_start,
                            "segment_end_seconds": seg_end,
                            "codec": codec,
                            "trim_exception_type": type(e).__name__,
                            "trim_exception": str(e),
                            "trim_error": exc_text,
                            "trim_data": None,
                            "final_output_created": False,
                            **cleanup,
                        },
                    )
                try:
                    trim_result = self._cut(
                        {
                            "input_path": str(seg_input),
                            "output_path": str(temp_path),
                            "start_seconds": seg_start if seg_start is not None else 0,
                            "end_seconds": seg_end,
                            "codec": codec,
                        }
                    )
                except Exception as e:
                    # Narrow segment-exception semantics (D2): preserve the
                    # segment index and the original exception context, halt
                    # without running later segments or the final join.
                    cleanup = _safe_cleanup()
                    exc_text = f"{type(e).__name__}: {e}"
                    return ToolResult(
                        success=False,
                        error=(
                            f"concat segment {i} trim raised {exc_text}; "
                            f"concat aborted before final join "
                            f"({len(segments)} segments requested). "
                            "No final artifact was created."
                        ),
                        data={
                            "operation": "concat",
                            "segment_count": len(segments),
                            "failed_segment_index": i,
                            "segment_input": str(seg_input),
                            "segment_start_seconds": seg_start,
                            "segment_end_seconds": seg_end,
                            "codec": codec,
                            "trim_exception_type": type(e).__name__,
                            "trim_exception": str(e),
                            "trim_error": exc_text,
                            "trim_data": None,
                            "final_output_created": False,
                            **cleanup,
                        },
                    )
                if not trim_result.success:
                    cleanup = _safe_cleanup()
                    return ToolResult(
                        success=False,
                        error=(
                            f"concat segment {i} trim failed; concat aborted "
                            f"before final join ({len(segments)} segments "
                            f"requested). Underlying trim error: "
                            f"{trim_result.error}"
                        ),
                        data={
                            "operation": "concat",
                            "segment_count": len(segments),
                            "failed_segment_index": i,
                            "segment_input": str(seg_input),
                            "segment_start_seconds": seg_start,
                            "segment_end_seconds": seg_end,
                            "codec": codec,
                            "trim_error": trim_result.error,
                            "trim_data": trim_result.data,
                            "final_output_created": False,
                            **cleanup,
                        },
                    )
                try:
                    trim_output_exists = temp_path.exists()
                except Exception:
                    trim_output_exists = False
                if not trim_output_exists:
                    # Defensive: _cut claimed success but left no file; do
                    # not let a missing entry reach the concat list.
                    cleanup = _safe_cleanup()
                    return ToolResult(
                        success=False,
                        error=(
                            f"concat segment {i} trim reported success but "
                            f"its output is missing ({temp_path}); concat "
                            f"aborted before final join ({len(segments)} "
                            "segments requested). No final artifact was "
                            "created."
                        ),
                        data={
                            "operation": "concat",
                            "segment_count": len(segments),
                            "failed_segment_index": i,
                            "segment_input": str(seg_input),
                            "segment_start_seconds": seg_start,
                            "segment_end_seconds": seg_end,
                            "codec": codec,
                            "trim_error": None,
                            "trim_data": trim_result.data,
                            "final_output_created": False,
                            **cleanup,
                        },
                    )
                concat_inputs.append(temp_path)
            else:
                concat_inputs.append(seg_input)

        # Write concat file list (temp-owned; failure still cleans temps and
        # preserves the primary list-write error). The list location is
        # collision-safe: a pre-existing original input at the default list
        # path is never overwritten — an alternative name is chosen — and
        # only the path actually created here is cleanup-owned (O1). The
        # temp dir itself is created lazily here when no trimmed segment
        # required it earlier.
        try:
            _ensure_temp_dir()
        except Exception as e:
            cleanup = _safe_cleanup()
            exc_text = f"{type(e).__name__}: {e}"
            return ToolResult(
                success=False,
                error=(
                    f"concat list write failed: {exc_text}; concat aborted "
                    f"before final join ({len(segments)} segments requested). "
                    "No final artifact was created."
                ),
                data={
                    "operation": "concat",
                    "segment_count": len(segments),
                    "output": str(output_path),
                    "join_error": exc_text,
                    "join_exception_type": type(e).__name__,
                    "final_output_created": False,
                    **cleanup,
                },
            )
        list_candidate = temp_dir / "concat_list.txt"
        list_counter = 0
        while _is_original(list_candidate):
            list_counter += 1
            if list_counter > 999:
                cleanup = _safe_cleanup()
                return ToolResult(
                    success=False,
                    error=(
                        "concat list write failed: cannot choose a "
                        "collision-safe list path; concat aborted before "
                        f"final join ({len(segments)} segments requested). "
                        "No final artifact was created."
                    ),
                    data={
                        "operation": "concat",
                        "segment_count": len(segments),
                        "output": str(output_path),
                        "join_error": "collision-safe list path unavailable",
                        "join_exception_type": "RuntimeError",
                        "final_output_created": False,
                        **cleanup,
                    },
                )
            list_candidate = temp_dir / f"concat_list_c{list_counter:02d}.txt"
        list_path_used = list_candidate
        list_created_by_us = True
        try:
            with open(list_path_used, "w", encoding="utf-8") as f:
                for tf in concat_inputs:
                    # FFmpeg concat demuxer needs forward slashes and escaped quotes
                    safe_path = str(tf.resolve()).replace("\\", "/")
                    f.write(f"file '{safe_path}'\n")
        except Exception as e:
            cleanup = _safe_cleanup()
            exc_text = f"{type(e).__name__}: {e}"
            return ToolResult(
                success=False,
                error=(
                    f"concat list write failed: {exc_text}; concat aborted "
                    f"before final join ({len(segments)} segments requested). "
                    "No final artifact was created."
                ),
                data={
                    "operation": "concat",
                    "segment_count": len(segments),
                    "output": str(output_path),
                    "join_error": exc_text,
                    "join_exception_type": type(e).__name__,
                    "final_output_created": False,
                    **cleanup,
                },
            )

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path_used),
            "-c", "copy",
            str(output_path),
        ]
        try:
            self.run_command(cmd)
        except Exception as e:
            # ---- Partial final output ownership (O4) ----
            # The output path is user-requested, NOT temp-owned. If this
            # invocation created a partial file and the join failed, attempt
            # removal and report the outcome truthfully. A pre-existing user
            # file is never deleted here; its survival is reported
            # explicitly instead.
            try:
                output_exists_after_join = output_path.exists()
            except Exception:
                output_exists_after_join = True
            created_by_us = (
                (not output_existed_before) and output_exists_after_join
            )
            partial_data: dict[str, Any] = {
                "output_existed_before": output_existed_before,
                "output_created_by_invocation": created_by_us,
            }
            partial_removal_text: Optional[str] = None
            if created_by_us:
                try:
                    _reset_remove_error()
                    p_removed, p_exists_after = _try_remove_artifact(
                        output_path
                    )
                    p_diag = (
                        _last_remove_error()
                        if ((not p_removed) or p_exists_after)
                        else None
                    )
                except Exception as e2:
                    p_removed, p_exists_after = False, True
                    p_diag = {
                        "error_type": type(e2).__name__,
                        "error_message": str(e2),
                    }
                partial_data["partial_output_removed"] = bool(p_removed)
                partial_data["partial_output_exists_after"] = bool(
                    p_exists_after
                )
                if p_diag is not None:
                    partial_data["partial_output_error_type"] = p_diag[
                        "error_type"
                    ]
                    partial_data["partial_output_error_message"] = p_diag[
                        "error_message"
                    ]
                    partial_data["partial_output_cleanup_error"] = (
                        f"{p_diag['error_type']}: {p_diag['error_message']}"
                    )
                    partial_removal_text = (
                        f"{output_path}: partial join output removal failed: "
                        f"{p_diag['error_type']}: {p_diag['error_message']}"
                    )
                elif p_exists_after:
                    partial_removal_text = (
                        f"{output_path}: partial join output still exists "
                        f"(removed={p_removed})"
                    )
            elif output_exists_after_join:
                partial_data["partial_output_removed"] = None
                partial_data["partial_output_exists_after"] = True
                if output_existed_before:
                    partial_data["partial_output_note"] = (
                        "pre-existing output file left in place; the failed "
                        "join may have truncated or overwritten it"
                    )
            else:
                partial_data["partial_output_removed"] = None
                partial_data["partial_output_exists_after"] = False
            cleanup = _safe_cleanup()
            if partial_removal_text is not None or (
                created_by_us
                and partial_data.get("partial_output_exists_after")
            ):
                # A partial file created by this invocation still exists:
                # surface it in the cleanup state truthfully (O4).
                try:
                    rem_paths = list(
                        cleanup.get("cleanup_remaining_paths") or []
                    )
                    if str(output_path) not in rem_paths:
                        rem_paths.append(str(output_path))
                    cleanup["cleanup_remaining_paths"] = rem_paths
                    cleanup["cleanup_ok"] = False
                    if partial_removal_text is not None:
                        prev_err = cleanup.get("cleanup_error")
                        cleanup["cleanup_error"] = (
                            (prev_err + "; " if prev_err else "")
                            + partial_removal_text
                        )
                except Exception:
                    try:
                        cleanup["cleanup_ok"] = False
                    except Exception:
                        pass
            exc_text = f"{type(e).__name__}: {e}"
            return ToolResult(
                success=False,
                error=f"concat join failed: {e}",
                data={
                    "operation": "concat",
                    "segment_count": len(segments),
                    "output": str(output_path),
                    "join_error": str(e),
                    "join_exception_type": type(e).__name__,
                    "join_exception": exc_text,
                    "final_output_created": False,
                    **partial_data,
                    **cleanup,
                },
            )

        cleanup = _safe_cleanup()
        return ToolResult(
            success=True,
            data={
                "operation": "concat",
                "segment_count": len(segments),
                "output": str(output_path),
                **cleanup,
            },
            artifacts=[str(output_path)],
        )

    @staticmethod
    def _build_atempo_chain(factor: float) -> str:
        """Build an atempo filter chain. atempo only accepts [0.5, 100.0]."""
        if factor <= 0:
            factor = 1.0
        # Chain multiple atempo filters for extreme values
        filters = []
        remaining = factor
        while remaining > 100.0:
            filters.append("atempo=100.0")
            remaining /= 100.0
        while remaining < 0.5:
            filters.append("atempo=0.5")
            remaining /= 0.5
        filters.append(f"atempo={remaining:.4f}")
        return ",".join(filters)
