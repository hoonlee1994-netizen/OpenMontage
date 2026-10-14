"""Image and video upscaling tool using Real-ESRGAN.

Takes low-resolution images or video and produces higher-resolution output
(2x or 4x). For video, frames are extracted via FFmpeg, upscaled individually,
and reassembled into the output file.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi"}

MODELS = {
    "RealESRGAN_x4plus": {
        "description": "General-purpose photo/video upscaler (default)",
        "scale": 4,
    },
    "RealESRGAN_x4plus_anime_6B": {
        "description": "Optimised for anime/illustration content",
        "scale": 4,
    },
    "RealESRNet_x4plus": {
        "description": "Lighter network, faster but lower quality",
        "scale": 4,
    },
}


class Upscale(BaseTool):
    name = "upscale"
    version = "0.1.0"
    tier = ToolTier.ENHANCE
    capability = "enhancement"
    provider = "realesrgan"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL_GPU

    dependencies = ["python:realesrgan", "python:torch", "cmd:ffmpeg"]
    install_instructions = (
        "uv pip install realesrgan torch\n"
        "Works on: CUDA (NVIDIA), MPS (Apple Silicon M-series, macOS >= 12.3), CPU fallback.\n"
        "No separate CUDA build needed on macOS — uv pip install torch includes MPS support."
    )
    agent_skills = ["ffmpeg"]

    capabilities = [
        "image_upscale",
        "video_upscale",
        "face_aware_upscale",
    ]

    input_schema = {
        "type": "object",
        "required": ["input_path"],
        "properties": {
            "input_path": {"type": "string"},
            "output_path": {"type": "string"},
            "scale": {
                "type": "integer",
                "enum": [2, 4],
                "default": 4,
            },
            "model": {
                "type": "string",
                "enum": list(MODELS.keys()),
                "default": "RealESRGAN_x4plus",
            },
            "face_enhance": {
                "type": "boolean",
                "default": False,
                "description": "Use GFPGAN for face regions",
            },
            "denoise_strength": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "default": 0.5,
                "description": "Denoising strength (0 = no denoise, 1 = full)",
            },
        },
    }

    resource_profile = ResourceProfile(cpu_cores=2, ram_mb=4096, vram_mb=2048, disk_mb=2000)
    idempotency_key_fields = ["input_path", "scale", "model", "face_enhance", "denoise_strength"]
    side_effects = ["writes upscaled file to output_path"]
    user_visible_verification = [
        "Compare upscaled output with original for detail and artifact quality",
        "Verify faces look natural if face_enhance was enabled",
    ]

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> ToolStatus:
        # GPU-native path (ncnn/Vulkan on Mali) needs no PyTorch.
        try:
            from lib.gpu import superres

            if superres.available("RealESRGAN_x4plus"):
                return ToolStatus.AVAILABLE
        except Exception:
            pass
        try:
            from tools.enhancement._torchvision_compat import (
                ensure_torchvision_compat,
            )

            ensure_torchvision_compat()
            import torch  # noqa: F401
            from basicsr.archs.rrdbnet_arch import RRDBNet  # noqa: F401
            from realesrgan import RealESRGANer  # noqa: F401
            return ToolStatus.AVAILABLE
        except (ImportError, ModuleNotFoundError):
            return ToolStatus.UNAVAILABLE
        except Exception:
            return ToolStatus.UNAVAILABLE

    def _preflight(self) -> str | None:
        """Check the real execution imports. Return error string or None."""
        try:
            from tools.enhancement._torchvision_compat import (
                ensure_torchvision_compat,
            )

            ensure_torchvision_compat()
            import torch  # noqa: F401
            from basicsr.archs.rrdbnet_arch import RRDBNet  # noqa: F401
            from realesrgan import RealESRGANer  # noqa: F401
        except ImportError as e:
            return f"Missing dependency: {e}. Run: uv pip install realesrgan torch"
        except Exception as e:
            return f"Dependency check failed: {e}"
        return None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        input_path = Path(inputs["input_path"])
        if not input_path.exists():
            return ToolResult(success=False, error=f"Input not found: {input_path}")

        is_video = input_path.suffix.lower() in VIDEO_EXTENSIONS
        model_name = inputs.get("model", "RealESRGAN_x4plus")
        face_enhance = inputs.get("face_enhance", False)

        # GPU-first: Real-ESRGAN on ncnn/Vulkan (Mali) for the models that have
        # an exact-weight ncnn conversion; PyTorch otherwise.
        from lib.gpu import superres

        use_gpu, gpu_reason = superres.gpu_support(model_name, face_enhance=face_enhance)
        if use_gpu and not is_video:
            use_gpu, gpu_reason = self._gpu_image_check(input_path, model_name, face_enhance)

        # Dependency preflight BEFORE creating any output directories/files.
        if not use_gpu:
            preflight_error = self._preflight()
            if preflight_error is not None:
                return ToolResult(success=False, error=f"{preflight_error} (GPU path: {gpu_reason})")

        if is_video and shutil.which("ffmpeg") is None:
            return ToolResult(
                success=False,
                error="Missing dependency: ffmpeg not found. Install ffmpeg to upscale video.",
            )

        default_output = str(input_path.with_stem(f"{input_path.stem}_upscaled"))
        output_path = Path(inputs.get("output_path", default_output))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        scale = inputs.get("scale", 4)
        denoise_strength = inputs.get("denoise_strength", 0.5)

        start = time.time()

        gpu_failure = None
        result = None
        if use_gpu:
            try:
                if is_video:
                    result = self._upscale_video_gpu(input_path, output_path, scale, model_name)
                else:
                    result = self._upscale_image_gpu(input_path, output_path, scale, model_name)
            except Exception as e:
                from lib.gpu import runtime as gpu_runtime

                if gpu_runtime.mode() == "require":
                    return ToolResult(success=False, error=f"GPU upscale failed (OM_GPU_MODE=require): {e}")
                gpu_failure = str(e)
                preflight_error = self._preflight()
                if preflight_error is not None:
                    return ToolResult(success=False, error=f"GPU upscale failed ({e}); CPU path unavailable: {preflight_error}")
        if result is None:
            from lib.gpu import runtime as gpu_runtime

            if gpu_runtime.mode() == "require":
                return ToolResult(success=False, error=f"OM_GPU_MODE=require but GPU upscale unavailable: {gpu_reason}")
            try:
                if is_video:
                    result = self._upscale_video(
                        input_path, output_path, scale, model_name,
                        face_enhance, denoise_strength,
                    )
                else:
                    result = self._upscale_image(
                        input_path, output_path, scale, model_name,
                        face_enhance, denoise_strength,
                    )
            except Exception as e:
                return ToolResult(success=False, error=f"Upscale failed: {e}")
            result["gpu_execution"] = {
                "backend": "cpu",
                "engine": "pytorch",
                "fallback_reason": gpu_failure or gpu_reason,
            }

        elapsed = time.time() - start

        return ToolResult(
            success=True,
            data={
                "input": str(input_path),
                "output": str(output_path),
                "scale": scale,
                "model": model_name,
                "face_enhance": face_enhance,
                "type": "video" if is_video else "image",
                **result,
            },
            artifacts=[str(output_path)],
            duration_seconds=round(elapsed, 2),
        )

    # ------------------------------------------------------------------
    # Image upscaling
    # ------------------------------------------------------------------

    def _upscale_image(
        self,
        input_path: Path,
        output_path: Path,
        scale: int,
        model_name: str,
        face_enhance: bool,
        denoise_strength: float,
    ) -> dict[str, Any]:
        import cv2

        upsampler = self._build_upsampler(scale, model_name, denoise_strength, face_enhance)

        img = cv2.imread(str(input_path), cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError(f"Could not read image: {input_path}")

        output, _ = upsampler.enhance(img, outscale=scale)
        cv2.imwrite(str(output_path), output)

        h, w = output.shape[:2]
        return {"output_width": w, "output_height": h}

    # ------------------------------------------------------------------
    # Video upscaling
    # ------------------------------------------------------------------

    def _upscale_video(
        self,
        input_path: Path,
        output_path: Path,
        scale: int,
        model_name: str,
        face_enhance: bool,
        denoise_strength: float,
    ) -> dict[str, Any]:
        import cv2

        upsampler = self._build_upsampler(scale, model_name, denoise_strength, face_enhance)

        # Get source frame rate
        fps = self._get_video_fps(input_path)

        with tempfile.TemporaryDirectory() as tmpdir:
            frames_dir = Path(tmpdir) / "frames"
            upscaled_dir = Path(tmpdir) / "upscaled"
            frames_dir.mkdir()
            upscaled_dir.mkdir()

            # Extract frames
            self.run_command([
                "ffmpeg", "-y",
                "-i", str(input_path),
                str(frames_dir / "frame_%06d.png"),
            ])

            # Upscale each frame
            frame_files = sorted(frames_dir.glob("*.png"))
            total_frames = len(frame_files)

            for frame_file in frame_files:
                img = cv2.imread(str(frame_file), cv2.IMREAD_UNCHANGED)
                output, _ = upsampler.enhance(img, outscale=scale)
                cv2.imwrite(str(upscaled_dir / frame_file.name), output)

            # Reassemble with ffmpeg, copy audio from original
            reassemble_cmd = [
                "ffmpeg", "-y",
                "-framerate", str(fps),
                "-i", str(upscaled_dir / "frame_%06d.png"),
                "-i", str(input_path),
                "-map", "0:v",
                "-map", "1:a?",
                "-c:v", "libx264", "-crf", "18",
                "-c:a", "copy",
                "-pix_fmt", "yuv420p",
                str(output_path),
            ]
            self.run_command(reassemble_cmd)

        return {"total_frames": total_frames, "fps": fps}

    # ------------------------------------------------------------------
    # GPU-native path (ncnn / Vulkan on the Mali GPU)
    # ------------------------------------------------------------------

    @staticmethod
    def _gpu_image_check(input_path: Path, model_name: str, face_enhance: bool) -> tuple[bool, str]:
        import cv2

        from lib.gpu import superres

        img = cv2.imread(str(input_path), cv2.IMREAD_UNCHANGED)
        if img is None:
            return False, f"could not read image {input_path}"
        return superres.gpu_support(model_name, face_enhance=face_enhance, image=img)

    @staticmethod
    def _finish_scale(img, w_in: int, h_in: int, scale: int):
        """Match RealESRGANer.enhance(outscale=...): x4 network, Lanczos to target."""
        import cv2

        if scale == 4:
            return img
        return cv2.resize(img, (int(w_in * scale), int(h_in * scale)), interpolation=cv2.INTER_LANCZOS4)

    def _upscale_image_gpu(self, input_path: Path, output_path: Path, scale: int, model_name: str) -> dict[str, Any]:
        import cv2

        from lib.gpu import superres

        src = cv2.imread(str(input_path), cv2.IMREAD_UNCHANGED)
        h_in, w_in = src.shape[:2]
        with tempfile.TemporaryDirectory(prefix="om-sr-") as tmp:
            # ncnn reads 8-bit RGB PNG/JPG/WEBP; normalise grayscale inputs.
            src_png = Path(tmp) / "in.png"
            cv2.imwrite(str(src_png), cv2.cvtColor(src, cv2.COLOR_GRAY2BGR) if src.ndim == 2 else src)
            raw = Path(tmp) / "out.png"
            info = superres.run(src_png, raw, model_name)
            out = cv2.imread(str(raw), cv2.IMREAD_UNCHANGED)
            if src.ndim == 2:
                out = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
            out = self._finish_scale(out, w_in, h_in, scale)
            if not cv2.imwrite(str(output_path), out):
                raise RuntimeError(f"could not write {output_path}")
        h, w = out.shape[:2]
        return {"output_width": w, "output_height": h, "gpu_execution": info}

    def _upscale_video_gpu(self, input_path: Path, output_path: Path, scale: int, model_name: str) -> dict[str, Any]:
        from lib.gpu import superres

        fps = self._get_video_fps(input_path)
        with tempfile.TemporaryDirectory(prefix="om-sr-") as tmpdir:
            frames_dir = Path(tmpdir) / "frames"
            upscaled_dir = Path(tmpdir) / "upscaled"
            frames_dir.mkdir()
            upscaled_dir.mkdir()
            self.run_command([
                "ffmpeg", "-y", "-i", str(input_path), str(frames_dir / "frame_%06d.png"),
            ])
            total_frames = len(list(frames_dir.glob("*.png")))
            if total_frames == 0:
                raise RuntimeError("no frames extracted")
            # One process for the whole batch: the model is loaded once and
            # PNG load/save overlaps with GPU inference.
            info = superres.run(frames_dir, upscaled_dir, model_name, threads=superres.threads_for_frames())
            produced = len(list(upscaled_dir.glob("*.png")))
            if produced != total_frames:
                raise RuntimeError(f"GPU upscaled {produced}/{total_frames} frames")
            vf = []
            if scale != 4:
                # Same Lanczos resampling RealESRGANer applies for outscale != 4.
                vf = ["-vf", f"scale=iw*{scale}/4:ih*{scale}/4:flags=lanczos"]
            self.run_command([
                "ffmpeg", "-y",
                "-framerate", str(fps),
                "-i", str(upscaled_dir / "frame_%06d.png"),
                "-i", str(input_path),
                "-map", "0:v",
                "-map", "1:a?",
                *vf,
                "-c:v", "libx264", "-crf", "18",
                "-c:a", "copy",
                "-pix_fmt", "yuv420p",
                str(output_path),
            ])
        return {"total_frames": total_frames, "fps": fps, "gpu_execution": info}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_upsampler(
        self,
        scale: int,
        model_name: str,
        denoise_strength: float,
        face_enhance: bool,
    ):
        """Build and return a RealESRGANer instance."""
        import inspect

        from tools.enhancement._torchvision_compat import (
            ensure_torchvision_compat,
        )

        ensure_torchvision_compat()

        import torch
        from basicsr.archs.rrdbnet_arch import RRDBNet
        from realesrgan import RealESRGANer

        # Select architecture based on model
        if model_name == "RealESRGAN_x4plus_anime_6B":
            model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=6, num_grow_ch=32, scale=4)
        else:
            model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=4)

        # Resolve model path — realesrgan ships weights or downloads them
        model_url = f"https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/{model_name}.pth"
        if model_name == "RealESRGAN_x4plus_anime_6B":
            model_url = f"https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/{model_name}.pth"

        from tools.video._shared import get_torch_device as _get_device
        _device = _get_device()
        half = _device == "cuda"  # fp16 only safe on CUDA; MPS/CPU use fp32 for realesrgan

        upsampler_kwargs: dict = {
            "scale": 4,
            "model_path": model_url,
            "model": model,
            "dni_weight": denoise_strength,
            "half": half,
            # Full-frame x4 inference can terminate the process on low-memory
            # CPU/MPS hosts before Python can raise an exception. Bound the
            # working set there; keep CUDA on the faster single-pass path.
            "tile": 0 if _device == "cuda" else 256,
            "tile_pad": 10,
        }
        # Guard: only pass device= if the installed version accepts it
        if "device" in inspect.signature(RealESRGANer.__init__).parameters:
            upsampler_kwargs["device"] = torch.device(_device)

        upsampler = RealESRGANer(**upsampler_kwargs)

        if face_enhance:
            from gfpgan import GFPGANer
            face_kwargs: dict = {
                "model_path": "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.3.pth",
                "upscale": scale,
                "arch": "clean",
                "channel_multiplier": 2,
                "bg_upsampler": upsampler,
            }
            # Guard: only pass device= if the installed version accepts it
            if "device" in inspect.signature(GFPGANer.__init__).parameters:
                face_kwargs["device"] = torch.device(_device)

            face_enhancer = GFPGANer(**face_kwargs)
            # Monkey-patch so the caller can use the same interface
            original_enhance = upsampler.enhance

            def enhance_with_face(img, outscale=scale):
                _, _, output = face_enhancer.enhance(
                    img, has_aligned=False, only_center_face=False, paste_back=True,
                )
                return output, None

            upsampler.enhance = enhance_with_face

        return upsampler

    def _get_video_fps(self, video_path: Path) -> float:
        """Extract frame rate from video using ffprobe."""
        import json

        if not shutil.which("ffprobe"):
            return 30.0  # safe default

        try:
            proc = self.run_command([
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                str(video_path),
            ])
            probe = json.loads(proc.stdout)
            for stream in probe.get("streams", []):
                if stream.get("codec_type") == "video":
                    r_frame_rate = stream.get("r_frame_rate", "30/1")
                    num, den = r_frame_rate.split("/")
                    return round(int(num) / int(den), 3)
        except Exception:
            pass

        return 30.0
