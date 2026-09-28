"""B5: character_rig_renderer optional video-preview readiness contract.

The default/package-only path requires neither Python Playwright nor
Chromium nor FFmpeg. The optional render_video path requires all three.
These tests prove the requirement is declared, preflighted, and fails
cleanly without raw exceptions or false artifacts.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import tools.character.character_animation as character_module
from tools.base_tool import ToolStatus
from tools.character.character_animation import CharacterRigRenderer
from tools.tool_registry import registry


def _minimal_inputs(tmp_path, **overrides):
    timeline = {
        "version": "1.0",
        "fps": 12,
        "scenes": [
            {
                "scene_id": "s1",
                "start_seconds": 0,
                "end_seconds": 1,
                "camera": {"framing": "medium"},
                "background": "test",
                "effects": [],
                "actions": [
                    {
                        "at_seconds": 0,
                        "duration_seconds": 0.5,
                        "character_id": "main_character",
                        "action": "perform",
                        "pose": "idle",
                        "easing": "power2.out",
                    }
                ],
            }
        ],
    }
    rig = {
        "version": "1.0",
        "characters": [{"character_id": "main_character"}],
        "metadata": {"source": "b5-test"},
    }
    inputs = {
        "action_timeline": timeline,
        "rig_plan": rig,
        "output_path": str(tmp_path / "preview.html"),
        "workspace_path": str(tmp_path / "hyperframes"),
    }
    inputs.update(overrides)
    return inputs


def test_conditional_dependency_metadata_visible_in_get_info():
    registry.discover()
    tool = registry.get("character_rig_renderer")
    info = tool.get_info()
    supports = info.get("supports", {})
    assert "render_package" in supports
    assert supports["render_package"].get("available_without_browser") is True
    assert "render_video" in supports
    conditional = supports["render_video"].get("conditional_dependencies", [])
    assert "python:playwright" in conditional
    assert "playwright:chromium" in conditional
    assert "cmd:ffmpeg" in conditional
    # Global dependencies must NOT include the conditional video runtime.
    assert tool.dependencies == []


def test_package_only_available_without_playwright(tmp_path):
    tool = CharacterRigRenderer()
    assert tool.get_status() == ToolStatus.AVAILABLE
    result = tool.execute(_minimal_inputs(tmp_path))
    assert result.success, result.error
    assert Path(result.data["preview_path"]).exists()
    assert Path(result.data["hyperframes_workspace"], "hyperframes.json").exists()
    assert Path(result.data["composition_path"]).exists()
    assert "video_path" not in result.data


def test_render_video_missing_python_module(tmp_path, monkeypatch):
    monkeypatch.setattr(
        character_module, "_is_python_playwright_available", lambda: False
    )
    monkeypatch.setattr(character_module, "_is_ffmpeg_available", lambda: True)
    tool = CharacterRigRenderer()
    readiness = tool.check_video_readiness()
    assert readiness["ready"] is False
    assert readiness["missing"] == ["python:playwright"]
    result = tool.execute(
        _minimal_inputs(
            tmp_path,
            render_video=True,
            video_output_path=str(tmp_path / "preview.mp4"),
        )
    )
    assert result.success is False
    assert result.data["requested_operation"] == "render_video"
    assert "python:playwright" in result.data["missing_dependencies"]
    assert "video_path" not in result.data
    assert not (tmp_path / "preview.mp4").exists()
    # Fails BEFORE package side effects.
    assert not (tmp_path / "preview.html").exists()
    assert result.artifacts == []


def test_render_video_missing_chromium(tmp_path, monkeypatch):
    monkeypatch.setattr(
        character_module, "_is_python_playwright_available", lambda: True
    )
    monkeypatch.setattr(character_module, "_is_ffmpeg_available", lambda: True)
    monkeypatch.setattr(
        character_module,
        "_probe_chromium_launchable",
        lambda: (False, "Playwright Chromium not launchable: fake missing executable"),
    )
    tool = CharacterRigRenderer()
    readiness = tool.check_video_readiness()
    assert readiness["ready"] is False
    assert readiness["missing"] == ["playwright:chromium"]
    result = tool.execute(
        _minimal_inputs(tmp_path, render_video=True,
                        video_output_path=str(tmp_path / "preview.mp4"))
    )
    assert result.success is False
    assert result.data["missing_dependencies"] == ["playwright:chromium"]
    assert "chromium" in (result.error or "").lower()
    # Must name the browser layer, not collapse to generic playwright.
    assert "playwright:chromium" in (result.error or "")
    assert "video_path" not in result.data
    assert not (tmp_path / "preview.mp4").exists()


def test_render_video_missing_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(
        character_module, "_is_python_playwright_available", lambda: True
    )
    monkeypatch.setattr(character_module, "_is_ffmpeg_available", lambda: False)
    monkeypatch.setattr(
        character_module,
        "_probe_chromium_launchable",
        lambda: (True, "Playwright Chromium launched successfully"),
    )
    tool = CharacterRigRenderer()
    readiness = tool.check_video_readiness()
    assert readiness["ready"] is False
    assert "cmd:ffmpeg" in readiness["missing"]
    result = tool.execute(
        _minimal_inputs(tmp_path, render_video=True,
                        video_output_path=str(tmp_path / "preview.mp4"))
    )
    assert result.success is False
    assert "cmd:ffmpeg" in result.data["missing_dependencies"]
    assert "ffmpeg" in (result.error or "").lower()
    assert "video_path" not in result.data
    assert not (tmp_path / "preview.mp4").exists()


def test_render_video_no_raw_exception_and_guidance(tmp_path, monkeypatch):
    monkeypatch.setattr(
        character_module, "_is_python_playwright_available", lambda: False
    )
    tool = CharacterRigRenderer()
    try:
        result = tool.execute(
            _minimal_inputs(tmp_path, render_video=True,
                            video_output_path=str(tmp_path / "preview.mp4"))
        )
    except Exception as exc:  # pragma: no cover - must not happen
        raise AssertionError(f"raw exception leaked: {exc!r}")
    assert result.success is False
    assert isinstance(result.error, str) and result.error
    assert "render_video" in (result.error or "")
    assert "install" in (result.error or "").lower() or "pip install" in (
        result.error or ""
    )
    assert "No video was rendered" in (result.error or "")
    assert "video_path" not in result.data


def test_readiness_success_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        character_module, "_is_python_playwright_available", lambda: True
    )
    monkeypatch.setattr(character_module, "_is_ffmpeg_available", lambda: True)
    monkeypatch.setattr(
        character_module,
        "_probe_chromium_launchable",
        lambda: (True, "Playwright Chromium launched successfully"),
    )
    readiness = CharacterRigRenderer().check_video_readiness()
    assert readiness["ready"] is True
    assert readiness["missing"] == []


def test_browser_probe_closes_resources(monkeypatch):
    closed = {"count": 0}

    class FakeBrowser:
        def close(self):
            closed["count"] += 1

    class FakeChromium:
        def launch(self):
            return FakeBrowser()

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    import sys
    import types

    fake_sync = types.ModuleType("playwright.sync_api")
    fake_sync.sync_playwright = lambda: FakePlaywright()
    fake_pkg = types.ModuleType("playwright")
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync)
    ok, _ = character_module._probe_chromium_launchable()
    assert ok is True
    assert closed["count"] == 1


def test_package_artifacts_unchanged_by_b5(tmp_path):
    from schemas.artifacts import validate_artifact

    result = CharacterRigRenderer().execute(_minimal_inputs(tmp_path))
    assert result.success
    validate_artifact("asset_manifest", result.data["asset_manifest"])
    validate_artifact("edit_decisions", result.data["edit_decisions"])
    assert result.data["edit_decisions"]["render_runtime"] == "hyperframes"
    workspace = Path(result.data["hyperframes_workspace"])
    assert (workspace / "hyperframes.json").exists()
    assert (workspace / "compositions" / "character-scene.html").exists()
    assert (workspace / "DESIGN.md").exists()
    assert Path(result.data["preview_path"]).exists()
