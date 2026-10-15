"""FFmpeg compose delivers over an existing file only when the whole command succeeded.

L5 full-scope finding F1 noted the FFmpeg compose delivery path as exposed like
the Remotion one: its last command wrote straight onto the production output.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.video.video_compose import VideoCompose

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not available",
)


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def names(d):
    return sorted(p.name for p in Path(d).iterdir())


def make_clip(path: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=teal:s=320x180:d=2:r=30",
                    "-c:v", "libx264", "-crf", "28", "-pix_fmt", "yuv420p", "-g", "30", str(path)],
                   capture_output=True, check=True)


def compose(src, out, **extra):
    ed = {"version": "1.0", "render_runtime": "ffmpeg",
          "cuts": [{"id": "c1", "source": str(src), "in_seconds": 0, "out_seconds": 1}]}
    return VideoCompose().execute({"operation": "compose", "edit_decisions": ed, "output_path": str(out),
                                   "profile": "tiktok", **extra})


class Fake:
    """Stand-in for ``run_command`` that records each command it is given."""

    def __init__(self, fail=False, payload=b"NEW"):
        self.cmds, self.fail, self.payload = [], fail, payload

    def __call__(self, cmd, **kw):
        self.cmds.append(list(cmd))
        Path(cmd[-1]).write_bytes(self.payload)
        if self.fail:
            raise subprocess.CalledProcessError(1, cmd, stderr="boom")


@pytest.fixture(autouse=True)
def trust_bytes(monkeypatch):
    from lib.gpu import ffmpeg_exec

    monkeypatch.setattr(ffmpeg_exec, "_validate", lambda outputs: None)


def test_a_new_output_is_written_directly(tmp_path, monkeypatch):
    tool = VideoCompose()
    fake = Fake()
    monkeypatch.setattr(tool, "run_command", fake)
    out = tmp_path / "new.mp4"
    tool._run_ffmpeg_to(["ffmpeg", "-y", "-i", "in.mp4"], out)
    assert fake.cmds[0][-1] == str(out) and out.read_bytes() == b"NEW"


def test_an_existing_output_is_staged_and_replaced_only_on_success(tmp_path, monkeypatch):
    tool = VideoCompose()
    out = tmp_path / "approved.mp4"
    out.write_bytes(b"APPROVED")
    fake = Fake()
    monkeypatch.setattr(tool, "run_command", fake)
    tool._run_ffmpeg_to(["ffmpeg", "-y", "-i", "in.mp4"], out)
    assert fake.cmds[0][-1] != str(out) and os.path.basename(fake.cmds[0][-1]).startswith(".om-render-")
    assert out.read_bytes() == b"NEW" and names(tmp_path) == ["approved.mp4"]


def test_a_failed_command_leaves_the_existing_output_untouched(tmp_path, monkeypatch):
    tool = VideoCompose()
    out = tmp_path / "approved.mp4"
    out.write_bytes(b"APPROVED")
    monkeypatch.setattr(tool, "run_command", Fake(fail=True, payload=b"half"))
    with pytest.raises(subprocess.CalledProcessError):
        tool._run_ffmpeg_to(["ffmpeg", "-y", "-i", "in.mp4"], out)
    assert out.read_bytes() == b"APPROVED" and names(tmp_path) == ["approved.mp4"]


def test_writing_a_file_over_its_own_input_is_left_to_ffmpeg(tmp_path, monkeypatch):
    tool = VideoCompose()
    out = tmp_path / "same.mp4"
    out.write_bytes(b"APPROVED")
    fake = Fake(fail=True)
    monkeypatch.setattr(tool, "run_command", fake)
    with pytest.raises(subprocess.CalledProcessError):
        tool._run_ffmpeg_to(["ffmpeg", "-y", "-i", str(out)], out)
    assert fake.cmds[0][-1] == str(out)            # not staged: FFmpeg reports the error itself


def test_recomposing_over_an_existing_output_replaces_it_with_a_valid_video(tmp_path):
    src = tmp_path / "in.mp4"
    make_clip(src)
    out = tmp_path / "out.mp4"
    assert compose(src, out).success
    first = sha(out)
    assert compose(src, out).success                        # same path again: an authorized retry
    probe = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                     "stream=width,height", "-of", "csv=p=0", str(out)]).decode().strip()
    assert probe == "1080,1920" and sha(out) is not None
    assert names(tmp_path) == ["in.mp4", "out.mp4"]
    assert first  # the first render existed


def test_a_compose_whose_final_step_fails_keeps_the_previous_output(tmp_path, monkeypatch):
    src = tmp_path / "in.mp4"
    make_clip(src)
    out = tmp_path / "out.mp4"
    assert compose(src, out).success
    approved = sha(out)

    real = VideoCompose.run_command

    def failing_final_step(self, cmd, **kw):
        if os.path.basename(cmd[-1]).startswith(".om-render-"):    # the delivery step
            Path(cmd[-1]).write_bytes(b"a truncated file")
            raise subprocess.CalledProcessError(1, cmd, stderr="killed")
        return real(self, cmd, **kw)

    monkeypatch.setattr(VideoCompose, "run_command", failing_final_step)
    res = compose(src, out)
    assert not res.success
    assert sha(out) == approved                              # the approved render survived
    assert names(tmp_path) == ["in.mp4", "out.mp4"]          # no staged file, no compose temp dir


# --------------------------------------------------------------------------
# The approved-audio mux is part of the Remotion delivery
# --------------------------------------------------------------------------

def render_inputs(tmp_path, out, **extra):
    return {"edit_decisions": {"render_runtime": "remotion", "renderer_family": "explainer-data",
                               "cuts": [{"id": "c1", "source": "a1", "in_seconds": 0, "out_seconds": 2}]},
            "asset_manifest": {"assets": [{"id": "a1", "path": "/tmp/a1.mp4"}]},
            "output_path": str(out), **extra}


@pytest.fixture
def render_tool(monkeypatch):
    tool = VideoCompose()
    monkeypatch.setattr(tool, "_pre_compose_validation", lambda *a, **k: None)
    monkeypatch.setattr(tool, "_needs_remotion", lambda *a, **k: True)
    monkeypatch.setattr(tool, "_run_final_review", lambda *a, **k: {})
    return tool


def test_render_hands_the_audio_mux_to_the_delivery(render_tool, tmp_path, monkeypatch):
    from tools.base_tool import ToolResult

    audio = tmp_path / "mix.wav"
    audio.write_bytes(b"x")
    seen = {}

    def fake_remotion_render(inputs, finalize=None):
        seen["finalize"] = finalize
        return ToolResult(success=True, data={}, artifacts=[])

    muxed = []
    monkeypatch.setattr(render_tool, "_remotion_render", fake_remotion_render)
    monkeypatch.setattr(render_tool, "_mux_external_audio",
                        lambda video, a: (muxed.append((str(video), str(a))), ToolResult(success=True, data={}))[1])
    res = render_tool._render(render_inputs(tmp_path, tmp_path / "out.mp4", audio_path=str(audio)))
    assert res.success and res.data["has_mixed_audio"] is True
    assert muxed == []                                   # not run after the render: the delivery owns it
    seen["finalize"]("/staged/render.mp4")
    assert muxed == [("/staged/render.mp4", str(audio))]


def test_render_without_audio_passes_no_finalize_step(render_tool, tmp_path, monkeypatch):
    from tools.base_tool import ToolResult

    calls = []
    monkeypatch.setattr(render_tool, "_remotion_render",
                        lambda *a, **k: (calls.append((a, k)), ToolResult(success=True, data={}))[1])
    render_tool._render(render_inputs(tmp_path, tmp_path / "out.mp4"))
    assert len(calls) == 1 and "finalize" not in calls[0][1]


def test_a_failed_audio_mux_is_reported_as_such_and_is_a_delivery_failure(render_tool, tmp_path, monkeypatch):
    from tools.base_tool import ToolResult

    audio = tmp_path / "mix.wav"
    audio.write_bytes(b"x")
    monkeypatch.setattr(render_tool, "_mux_external_audio",
                        lambda video, a: ToolResult(success=False, error="Could not mux mixed audio: boom"))

    def fake_remotion_render(inputs, finalize=None):
        try:
            finalize("/staged/render.mp4")
        except VideoCompose._PostRenderFailure as pf:
            return pf.result                              # exactly what _remotion_render does
        return ToolResult(success=True, data={})

    monkeypatch.setattr(render_tool, "_remotion_render", fake_remotion_render)
    res = render_tool._render(render_inputs(tmp_path, tmp_path / "out.mp4", audio_path=str(audio)))
    assert not res.success and res.error == "Could not mux mixed audio: boom"   # not a "Remotion render failed"
