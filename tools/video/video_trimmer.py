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

        # Post-cut validation: a video input must yield a video stream.
        # Never report success for an audio-only cut of a video source.
        base_data: dict[str, Any] = {
            "operation": "cut",
            "input": str(input_path),
            "output": str(output_path),
            "start_seconds": start_s,
            "end_seconds": end_s,
            "codec": codec,
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
        if output_types is None:
            # Probe inconclusive — do not claim a verified success, but
            # leave the file in place for manual inspection.
            return ToolResult(
                success=False,
                error=(
                    "Cut output could not be stream-validated (ffprobe "
                    f"unavailable or failed); not claiming success. Output "
                    f"left at {output_path} for manual inspection."
                ),
                data=base_data,
            )
        if "video" in (input_types or set()) and "video" not in output_types:
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
