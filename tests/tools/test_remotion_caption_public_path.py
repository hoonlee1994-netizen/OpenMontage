"""B4 regression: Remotion caption-burn asset addressing must be public-relative.

Physical media is staged at ``remotion-composer/public/talking-head/<file>``.
Remotion ``staticFile()`` paths are relative to ``public/``, so the emitted
``videoSrc`` prop must be ``talking-head/<file>`` — never
``public/talking-head/<file>`` (Remotion throws
"Do not include the public/ prefix when using staticFile()").

Covers the B4 contract without touching unrelated compositions:
  1. staged media remains under public/talking-head/
  2. emitted videoSrc is public-relative (talking-head/<filename>)
  3. videoSrc does NOT start with public/ or /public/
  4. resolveAsset() remains compatible with the emitted value
  5. remote URL behavior in resolveAsset() is unchanged
  6. absolute-path behavior in resolveAsset() is unchanged
  7. FFmpeg fallback behavior is unchanged
  8. caption conversion behavior is unchanged
  9. overlay props remain unchanged
 10. output path/render command semantics remain unchanged
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.video.remotion_caption_burn import RemotionCaptionBurn  # noqa: E402

RESOLVE_ASSET = (
    PROJECT_ROOT / "remotion-composer" / "src" / "lib" / "resolveAsset.ts"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _make_tool(monkeypatch, tmp_path):
    """Tool with a fake remotion root and faked shell commands (no real render)."""
    tool = RemotionCaptionBurn()
    root = tmp_path / "remotion-composer"
    (root / "node_modules").mkdir(parents=True)
    (root / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tool, "_find_remotion_root", lambda: root)

    seen = {}

    def fake_run_command(cmd, *args, **kwargs):
        seen.setdefault("cmds", []).append(cmd)
        if cmd[0] == "ffprobe":
            if "format=duration" in cmd:
                return SimpleNamespace(stdout="3.0\n", stderr="")
            return SimpleNamespace(stdout="640x480", stderr="")
        # npx remotion render — simulate success by creating the output file.
        out = cmd[-1]
        assert out.startswith("--output=")
        Path(out.split("=", 1)[1]).touch()
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(tool, "run_command", fake_run_command)
    return tool, root, seen


def _sample_captions():
    return [
        {"word": "hello", "startMs": 0, "endMs": 500},
        {"word": "world", "startMs": 500, "endMs": 1000},
    ]


def _py_resolve_asset_route(src: str) -> str:
    """Python mirror of resolveAsset.ts routing (which branch handles src)."""
    if src.startswith(("http://", "https://", "data:")):
        return "remote"
    clean = re.sub(r"^file://", "", src, flags=re.IGNORECASE)
    if re.match(r"^/[A-Za-z]:[\\/]", clean):
        clean = clean[1:]
    if clean.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", clean):
        return "absolute"
    return "staticFile"


# --------------------------------------------------------------------------- #
# 1–3, 9–10: producer boundary — staging path, videoSrc, overlays, render cmd
# --------------------------------------------------------------------------- #

def test_video_src_is_public_relative_and_media_staged_under_public(
    tmp_path, monkeypatch
):
    tool, root, seen = _make_tool(monkeypatch, tmp_path)
    src = tmp_path / "input.mp4"
    src.write_bytes(b"fake-video")
    out = tmp_path / "out.mp4"
    overlays = [
        {
            "type": "text_card",
            "text": "hi",
            "in_seconds": 0.0,
            "out_seconds": 1.0,
        }
    ]

    result = tool._render_remotion(
        str(src), str(out), _sample_captions(), 4, 52, "#22D3EE",
        overlays=overlays,
    )

    assert result.success is True
    # 1. staged media remains under public/talking-head/
    staged = root / "public" / "talking-head" / "input.mp4"
    assert staged.is_file()
    # 2/3. videoSrc is public-relative, with no public/ prefix
    props_file = root / "public" / "demo-props" / "caption-burn-input.json"
    props = json.loads(props_file.read_text(encoding="utf-8"))
    assert props["videoSrc"] == "talking-head/input.mp4"
    assert not props["videoSrc"].startswith("public/")
    assert not props["videoSrc"].startswith("/public/")
    # 9. overlay props pass through unchanged
    assert props["overlays"] == overlays
    assert props["captions"] == _sample_captions()
    assert props["wordsPerPage"] == 4
    assert props["fontSize"] == 52
    assert props["highlightColor"] == "#22D3EE"
    # 10. output path / render command semantics unchanged
    render_cmd = seen["cmds"][-1]
    assert render_cmd[1:4] == ["remotion", "render", "TalkingHead"]
    assert f"--output={str(out.resolve())}" in render_cmd
    assert "--codec=h264" in render_cmd
    assert any(c.startswith("--props=public/demo-props/") for c in render_cmd)
    assert result.data["method"] == "remotion"


# --------------------------------------------------------------------------- #
# 4–6: resolveAsset() consumer compatibility (source-anchored, no TS runtime)
# --------------------------------------------------------------------------- #

def test_resolve_asset_source_semantics_unchanged():
    text = RESOLVE_ASSET.read_text(encoding="utf-8")
    # Remote URLs pass through untouched.
    assert 'src.startsWith("http://")' in text
    assert 'src.startsWith("https://")' in text
    assert 'src.startsWith("data:")' in text
    # Absolute paths become file:// URLs.
    assert "file://" in text
    # Relative paths fall through to staticFile(clean).
    assert "return staticFile(clean);" in text


def test_emitted_video_src_routes_to_static_file_branch():
    # 4. the B4 value must hit the staticFile() branch, not remote/absolute.
    assert _py_resolve_asset_route("talking-head/input.mp4") == "staticFile"
    # The old defective value never reaches staticFile — Remotion throws.
    assert _py_resolve_asset_route("public/talking-head/input.mp4") == "staticFile"
    # 5. remote URL behavior unchanged.
    assert (
        _py_resolve_asset_route("https://example.com/video.mp4") == "remote"
    )
    # 6. absolute-path behavior unchanged.
    assert _py_resolve_asset_route("/abs/path/video.mp4") == "absolute"
    assert _py_resolve_asset_route("C:\\videos\\v.mp4") == "absolute"


# --------------------------------------------------------------------------- #
# 7: FFmpeg fallback unchanged
# --------------------------------------------------------------------------- #

def test_ffmpeg_fallback_unchanged(tmp_path, monkeypatch):
    tool = RemotionCaptionBurn()
    seen = {}

    def fake_run_command(cmd, *args, **kwargs):
        seen["cmd"] = cmd
        Path(cmd[-1]).touch()
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(tool, "run_command", fake_run_command)
    result = tool._render_ffmpeg(
        "in.mp4", str(tmp_path / "out.mp4"), _sample_captions()
    )

    assert result.success is True
    assert result.data["method"] == "ffmpeg_fallback"
    cmd = seen["cmd"]
    assert cmd[:3] == ["ffmpeg", "-y", "-i"]
    assert any(c.startswith("subtitles=") for c in " ".join(cmd).split())
    assert "-c:v" in cmd and "libx264" in cmd
    assert "-c:a" in cmd and "copy" in cmd


def test_execute_force_ffmpeg_routes_to_fallback(tmp_path, monkeypatch):
    tool = RemotionCaptionBurn()
    src = tmp_path / "in.mp4"
    src.write_bytes(b"x")
    monkeypatch.setattr(
        tool, "_render_ffmpeg",
        lambda *a, **k: __import__(
            "tools.base_tool", fromlist=["ToolResult"]
        ).ToolResult(success=True, data={"method": "ffmpeg_fallback"}),
    )
    segments = [{"words": [{"word": "hi", "start": 0.0, "end": 0.5}]}]
    result = tool.execute({
        "input_path": str(src),
        "output_path": str(tmp_path / "o.mp4"),
        "segments": segments,
        "force_ffmpeg": True,
    })
    assert result.success is True
    assert result.data["method"] == "ffmpeg_fallback"


# --------------------------------------------------------------------------- #
# 8: caption conversion unchanged
# --------------------------------------------------------------------------- #

def test_segments_to_word_captions_unchanged():
    tool = RemotionCaptionBurn()
    segments = [
        {"words": [
            {"word": "hello,", "start": 0.0, "end": 0.5},
            {"word": "world", "start": 0.5, "end": 1.0},
        ]},
        {"text": "foo bar", "start": 1.0, "end": 2.0},
    ]
    captions = tool._segments_to_word_captions(
        segments, corrections={"hello": "hallo"}
    )
    assert captions[0] == {"word": "hallo,", "startMs": 0, "endMs": 500}
    assert captions[1] == {"word": "world", "startMs": 500, "endMs": 1000}
    # text-only segment distributes duration evenly across words
    assert captions[2] == {"word": "foo", "startMs": 1000, "endMs": 1500}
    assert captions[3] == {"word": "bar", "startMs": 1500, "endMs": 2000}


def test_srt_to_word_captions_unchanged(tmp_path):
    tool = RemotionCaptionBurn()
    srt = tmp_path / "c.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhello world\n",
        encoding="utf-8",
    )
    captions = tool._srt_to_word_captions(str(srt))
    assert captions == [
        {"word": "hello", "startMs": 0, "endMs": 500},
        {"word": "world", "startMs": 500, "endMs": 1000},
    ]
