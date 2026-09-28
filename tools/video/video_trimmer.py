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


# Copy-mode timing contract (B1 corrective repair).
#
# Stream-copy (`codec="copy"`) with input seeking (`-ss` before `-i`) starts
# from the preceding keyframe, so the produced window can be materially
# longer/shifted versus the requested [start, end) interval. The tool must
# not silently report such a window as success.
#
# Contract: for codec="copy" with an explicit end_seconds, the ffprobe
# format duration of the output must satisfy
#   |actual_duration - requested_duration| <= COPY_DURATION_TOLERANCE_SEC
# otherwise the cut fails closed with a re-encode recommendation. Re-encode
# mode is frame-accurate and is not subject to this gate.
#
# Tolerance rationale: dense-GOP fixtures (keyframe every ~0.4s) show
# packet-granularity jitter of ~0.17-0.27s on valid copy cuts, while the
# failing sparse-GOP 1.7-4.3s case overshoots by ~1.87s (2.6s requested vs
# ~4.47s actual). 0.5s accepts the former and rejects the latter with
# margin on both sides.
COPY_DURATION_TOLERANCE_SEC = 0.5


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

        # Post-cut validation (B1 fail-safe contract):
        # - success=True must mean the output is PROVEN to contain video
        #   whenever the trim is a video trim.
        # - input probe None/empty is UNKNOWN, distinct from positively
        #   identified audio-only. Never infer audio-only from unknown.
        # - copy mode must satisfy the requested window within
        #   COPY_DURATION_TOLERANCE_SEC or fail with a re-encode direction.
        # Never use filename extensions as proof of stream type.
        requested_duration: Optional[float] = None
        if end_s is not None:
            try:
                requested_duration = float(end_s) - float(start_s)
            except (TypeError, ValueError):
                requested_duration = None
        base_data: dict[str, Any] = {
            "operation": "cut",
            "input": str(input_path),
            "output": str(output_path),
            "start_seconds": start_s,
            "end_seconds": end_s,
            "codec": codec,
            "requested_duration": requested_duration,
            "copy_duration_tolerance_sec": (
                COPY_DURATION_TOLERANCE_SEC if codec == "copy" else None
            ),
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
        input_types = probe_codec_types(input_path)
        output_types = probe_codec_types(output_path)
        base_data["input_streams"] = sorted(input_types) if input_types is not None else None
        base_data["output_streams"] = sorted(output_types) if output_types is not None else None
        # Probe state classification: None or empty means UNKNOWN, never
        # "known audio-only". Only a non-empty set without "video" is
        # positively identified audio-only (or at least non-video).
        input_unknown = input_types is None or len(input_types) == 0
        input_known_video = (
            not input_unknown and "video" in (input_types or set())
        )
        input_known_audio_only = (
            not input_unknown and "video" not in (input_types or set())
        )
        base_data["input_probe"] = (
            "known_video" if input_known_video
            else "known_audio_only" if input_known_audio_only
            else "unknown"
        )
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
        if "video" not in output_types:
            # Output is provably audio-only (or at least non-video).
            if input_unknown:
                # Fail closed: input might have been video; an audio-only
                # result must never pass as a successful video trim. Do not
                # infer the source was audio-only. Remove the artifact so a
                # false success cannot be consumed downstream.
                try:
                    output_path.unlink()
                except OSError:
                    pass
                return ToolResult(
                    success=False,
                    error=(
                        "Cut produced audio-only output while the input "
                        "stream probe is unknown/inconclusive "
                        f"(input_streams={base_data['input_streams']}; "
                        f"output_streams={sorted(output_types)}). Video "
                        "preservation cannot be established, so success "
                        "is denied (fail-closed). This unknown-probe state "
                        "is distinct from positively identified audio-only "
                        "input. The invalid output was removed; re-probe "
                        "the input or retry with codec='libx264' "
                        "(re-encode) once the source streams are known."
                    ),
                    data=base_data,
                )
            if input_known_video:
                # Copy mode dropped the video stream (typically a non-keyframe
                # cut). Never report success; remove the invalid artifact and
                # point at re-encode mode. Do NOT silently fall back to a lossy
                # re-encode of an explicitly requested codec="copy" operation.
                try:
                    output_path.unlink()
                except OSError:
                    pass
                return ToolResult(
                    success=False,
                    error=(
                        "copy-mode cut produced no video stream (input has "
                        f"video; output streams: {sorted(output_types)}). "
                        "Stream-copy cannot cut accurately at this position; "
                        "retry with codec='libx264' (re-encode) instead of "
                        "codec='copy'. The invalid output was removed."
                    ),
                    data=base_data,
                )
            # Positively identified audio-only input -> audio-only output is
            # the supported path.
            base_data["output_has_video"] = False
            return ToolResult(
                success=True,
                data=base_data,
                artifacts=[str(output_path)],
            )
        # Output provably contains video. For copy mode with an explicit
        # window, additionally enforce the timing contract.
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
            if delta > COPY_DURATION_TOLERANCE_SEC:
                try:
                    output_path.unlink()
                except OSError:
                    pass
                return ToolResult(
                    success=False,
                    error=(
                        "copy-mode cut cannot satisfy the requested "
                        f"[{start_s}, {end_s}] window within tolerance "
                        f"(requested {requested_duration:.3f}s, actual "
                        f"{actual_duration:.3f}s, delta {delta:.3f}s > "
                        f"tolerance {COPY_DURATION_TOLERANCE_SEC:.3f}s). "
                        "Stream-copy starts from the preceding keyframe and "
                        "is not frame-accurate; retry with codec='libx264' "
                        "(re-encode) instead of codec='copy'. The invalid "
                        "output was removed."
                    ),
                    data=base_data,
                )
        else:
            base_data["actual_duration"] = (
                probe_output_duration(output_path)
                if "video" in output_types else None
            )

        base_data["output_has_video"] = "video" in output_types
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
        segments = inputs.get("segments", [])
        if not segments:
            return ToolResult(success=False, error="No segments provided for concat")

        output_path = Path(inputs.get("output_path", "concat_output.mp4"))

        # First, cut each segment to a temp file if start/end are specified
        temp_files: list[Path] = []
        temp_dir = output_path.parent / ".concat_tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            for i, seg in enumerate(segments):
                seg_input = Path(seg["input_path"])
                if not seg_input.exists():
                    return ToolResult(success=False, error=f"Segment input not found: {seg_input}")

                seg_start = seg.get("start_seconds")
                seg_end = seg.get("end_seconds")

                if seg_start is not None or seg_end is not None:
                    temp_path = temp_dir / f"seg_{i:04d}{seg_input.suffix}"
                    cmd = ["ffmpeg", "-y", "-i", str(seg_input)]
                    if seg_start is not None:
                        cmd.extend(["-ss", str(seg_start)])
                    if seg_end is not None:
                        cmd.extend(["-to", str(seg_end)])
                    cmd.extend(["-c", "copy", str(temp_path)])
                    self.run_command(cmd)
                    temp_files.append(temp_path)
                else:
                    temp_files.append(seg_input)

            # Write concat file list
            list_path = temp_dir / "concat_list.txt"
            with open(list_path, "w", encoding="utf-8") as f:
                for tf in temp_files:
                    # FFmpeg concat demuxer needs forward slashes and escaped quotes
                    safe_path = str(tf.resolve()).replace("\\", "/")
                    f.write(f"file '{safe_path}'\n")

            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_path),
                "-c", "copy",
                str(output_path),
            ]
            self.run_command(cmd)

            return ToolResult(
                success=True,
                data={
                    "operation": "concat",
                    "segment_count": len(segments),
                    "output": str(output_path),
                },
                artifacts=[str(output_path)],
            )
        finally:
            # Clean up temp segment files (but not the originals)
            for tf in temp_files:
                if tf.parent == temp_dir and tf.exists():
                    tf.unlink()
            if list_path.exists():
                list_path.unlink()
            if temp_dir.exists():
                try:
                    temp_dir.rmdir()
                except OSError:
                    pass

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
