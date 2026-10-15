"""HyperFrames renders over an existing deliverable are staged and published only on success."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lib.gpu import ffmpeg_exec
from tools.base_tool import ToolResult
from tools.video.hyperframes_compose import HyperFramesCompose


@pytest.fixture(autouse=True)
def opaque_media(monkeypatch):
    monkeypatch.setattr(ffmpeg_exec, "_validate", lambda outputs: None)


def make_tool(monkeypatch, tmp_path, *, rc=0, payload=b"NEW", write=True):
    workspace = tmp_path / "workspace"
    (workspace).mkdir(exist_ok=True)
    (workspace / "index.html").write_text("<html></html>")
    tool = HyperFramesCompose()
    seen = {}
    monkeypatch.setattr(tool, "_runtime_check", lambda: {"runtime_available": True})
    monkeypatch.setattr(tool, "_scaffold", lambda inputs: ToolResult(success=True, data={}))
    monkeypatch.setattr(tool, "_lint", lambda inputs: ToolResult(success=True, data={}))
    monkeypatch.setattr(tool, "_validate", lambda inputs: ToolResult(success=True, data={}))
    monkeypatch.setattr(tool, "_check", lambda inputs: ToolResult(success=True, data={}))

    def run_hf(args, *, cwd, timeout, check):
        out = Path(args[args.index("--output") + 1])
        seen["out"] = str(out)
        seen["final_during"] = (workspace.parent / "renders" / "final.mp4").read_bytes() \
            if (workspace.parent / "renders" / "final.mp4").exists() else None
        if write:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(payload)
        return subprocess.CompletedProcess(args, rc, "", "boom" if rc else "")

    monkeypatch.setattr(tool, "_run_hf", run_hf)
    return tool, workspace, seen


def names(d):
    return sorted(p.name for p in Path(d).iterdir())


@pytest.mark.parametrize("operation", ["render", "render_existing"])
def test_a_render_over_an_existing_file_is_staged_and_published(tmp_path, monkeypatch, operation):
    final = tmp_path / "renders" / "final.mp4"
    final.parent.mkdir()
    final.write_bytes(b"APPROVED")
    tool, workspace, seen = make_tool(monkeypatch, tmp_path)
    run = tool._render if operation == "render" else tool._render_existing
    res = run({"workspace_path": str(workspace), "output_path": str(final)})
    assert res.success, res.error
    assert seen["out"] != str(final) and Path(seen["out"]).name.startswith(".om-render-")
    assert seen["final_during"] == b"APPROVED"
    assert final.read_bytes() == b"NEW" and names(final.parent) == ["final.mp4"]


@pytest.mark.parametrize("operation", ["render", "render_existing"])
def test_a_failed_render_keeps_the_existing_file(tmp_path, monkeypatch, operation):
    final = tmp_path / "renders" / "final.mp4"
    final.parent.mkdir()
    final.write_bytes(b"APPROVED")
    tool, workspace, seen = make_tool(monkeypatch, tmp_path, rc=1, payload=b"half")
    run = tool._render if operation == "render" else tool._render_existing
    res = run({"workspace_path": str(workspace), "output_path": str(final)})
    assert not res.success
    assert final.read_bytes() == b"APPROVED" and names(final.parent) == ["final.mp4"]


@pytest.mark.parametrize("operation", ["render", "render_existing"])
def test_a_render_to_a_new_path_is_written_directly(tmp_path, monkeypatch, operation):
    final = tmp_path / "renders" / "final.mp4"
    tool, workspace, seen = make_tool(monkeypatch, tmp_path)
    run = tool._render if operation == "render" else tool._render_existing
    res = run({"workspace_path": str(workspace), "output_path": str(final)})
    assert res.success, res.error
    assert seen["out"] == str(final) and final.read_bytes() == b"NEW"


@pytest.mark.parametrize("operation", ["render", "render_existing"])
def test_a_render_that_exits_zero_without_output_is_a_failure(tmp_path, monkeypatch, operation):
    final = tmp_path / "renders" / "final.mp4"
    final.parent.mkdir()
    final.write_bytes(b"APPROVED")
    tool, workspace, seen = make_tool(monkeypatch, tmp_path, write=False)
    run = tool._render if operation == "render" else tool._render_existing
    res = run({"workspace_path": str(workspace), "output_path": str(final)})
    assert not res.success and "missing" in res.error
    assert final.read_bytes() == b"APPROVED"
