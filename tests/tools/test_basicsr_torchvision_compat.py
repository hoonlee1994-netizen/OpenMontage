"""B6: BasicSR / torchvision compatibility repair tests.

Covers the narrow ``functional_tensor`` -> ``functional.rgb_to_grayscale``
bridge, status truthfulness, execute failure semantics, and the conditional
``bg_upsampler`` path. All tests run without network or model downloads
(except the fresh-process import check, which only imports classes).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

LEGACY = "torchvision.transforms.functional_tensor"
MODERN = "torchvision.transforms.functional"
SYMBOL = "rgb_to_grayscale"


def _remove_legacy_alias():
    """Remove our shim if present; return what was there."""
    return sys.modules.pop(LEGACY, None)


def _restore_legacy_alias(previous):
    if previous is not None:
        sys.modules[LEGACY] = previous


# ------------------------------------------------------------------
# 1. legacy already available -> no-op
# ------------------------------------------------------------------

def test_legacy_already_available_is_noop(monkeypatch):
    from tools.enhancement import _torchvision_compat as compat

    sentinel = types.ModuleType(LEGACY)
    sentinel.rgb_to_grayscale = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, LEGACY, sentinel)

    assert compat.ensure_torchvision_compat() is False
    assert sys.modules[LEGACY] is sentinel


# ------------------------------------------------------------------
# 2. legacy missing + modern present -> alias works
# ------------------------------------------------------------------

def test_missing_legacy_with_modern_present_installs_alias():
    from tools.enhancement import _torchvision_compat as compat

    previous = _remove_legacy_alias()
    try:
        # Precondition: real env has no native legacy module.
        assert LEGACY not in sys.modules
        result = compat.ensure_torchvision_compat()
        assert result is True
        mod = sys.modules[LEGACY]
        assert hasattr(mod, SYMBOL)
        from torchvision.transforms.functional import rgb_to_grayscale

        assert mod.rgb_to_grayscale is rgb_to_grayscale
        # The previously failing BasicSR chain now imports.
        import basicsr  # noqa: F401
    finally:
        # Keep alias active for the rest of the suite (idempotent).
        pass


# ------------------------------------------------------------------
# 3. helper idempotent
# ------------------------------------------------------------------

def test_helper_idempotent():
    from tools.enhancement import _torchvision_compat as compat

    _remove_legacy_alias()
    first = compat.ensure_torchvision_compat()
    mod_first = sys.modules[LEGACY]
    second = compat.ensure_torchvision_compat()
    mod_second = sys.modules[LEGACY]
    assert first is True
    assert second is True
    assert mod_first is mod_second


# ------------------------------------------------------------------
# 4. torchvision missing -> truthful failure
# ------------------------------------------------------------------

def test_torchvision_missing_fails_truthfully(monkeypatch):
    from tools.enhancement import _torchvision_compat as compat

    monkeypatch.delitem(sys.modules, LEGACY, raising=False)

    real_import = importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == LEGACY:
            raise ModuleNotFoundError("No module named 'torchvision.transforms.functional_tensor'", name=LEGACY)
        if name == "torchvision":
            raise ModuleNotFoundError("No module named 'torchvision'", name="torchvision")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    # Also patch the compat module's reference (it uses `importlib.import_module`).
    monkeypatch.setattr(compat.importlib, "import_module", fake_import)

    with pytest.raises(ImportError, match="torchvision"):
        compat.ensure_torchvision_compat()


# ------------------------------------------------------------------
# 5. replacement function missing -> truthful failure
# ------------------------------------------------------------------

def test_replacement_missing_fails_truthfully(monkeypatch):
    from tools.enhancement import _torchvision_compat as compat

    monkeypatch.delitem(sys.modules, LEGACY, raising=False)

    fake_modern = types.ModuleType(MODERN)
    # Intentionally no rgb_to_grayscale attribute.
    monkeypatch.setitem(sys.modules, MODERN, fake_modern)

    # importlib.import_module returns the sys.modules entry when present.
    with pytest.raises(ImportError, match="rgb_to_grayscale"):
        compat.ensure_torchvision_compat()


# ------------------------------------------------------------------
# 6. unrelated exception not swallowed
# ------------------------------------------------------------------

def test_unrelated_exception_not_swallowed(monkeypatch):
    from tools.enhancement import _torchvision_compat as compat

    monkeypatch.delitem(sys.modules, LEGACY, raising=False)

    def boom(name, *args, **kwargs):
        if name == LEGACY:
            raise RuntimeError("unrelated boom inside legacy import")
        return importlib.import_module.__wrapped__(name, *args, **kwargs) if hasattr(importlib.import_module, "__wrapped__") else _real(name)

    _real = importlib.import_module

    def fake(name, *args, **kwargs):
        if name == LEGACY:
            raise RuntimeError("unrelated boom inside legacy import")
        return _real(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", fake)
    monkeypatch.setattr(compat.importlib, "import_module", fake)

    with pytest.raises(RuntimeError, match="unrelated boom"):
        compat.ensure_torchvision_compat()


# ------------------------------------------------------------------
# 7/8. status checks real execution imports
# ------------------------------------------------------------------

def test_upscale_status_checks_real_execution_imports(monkeypatch):
    from tools.base_tool import ToolStatus
    from tools.enhancement.upscale import Upscale

    # Simulate RRDBNet broken while top-level realesrgan exists.
    monkeypatch.setitem(sys.modules, "basicsr.archs.rrdbnet_arch", None)
    # Ensure submodule import fails: remove any cached good copy first.
    for key in [k for k in sys.modules if k.startswith("basicsr.archs.rrdbnet_arch")]:
        monkeypatch.delitem(sys.modules, key, raising=False)
    monkeypatch.setitem(sys.modules, "basicsr.archs.rrdbnet_arch", None)

    tool = Upscale()
    assert tool.get_status() == ToolStatus.UNAVAILABLE


def test_face_restore_status_checks_real_execution_imports(monkeypatch):
    from tools.base_tool import ToolStatus
    from tools.enhancement.face_restore import FaceRestore

    monkeypatch.setitem(sys.modules, "gfpgan", None)
    tool = FaceRestore()
    assert tool.get_status() == ToolStatus.UNAVAILABLE


# ------------------------------------------------------------------
# 9. versions not mutated
# ------------------------------------------------------------------

def test_main_environment_versions_not_mutated():
    import importlib.metadata as md

    from tools.enhancement._torchvision_compat import ensure_torchvision_compat

    before = {
        "torch": md.version("torch"),
        "torchvision": md.version("torchvision"),
        "basicsr": md.version("basicsr"),
    }
    ensure_torchvision_compat()
    after = {
        "torch": md.version("torch"),
        "torchvision": md.version("torchvision"),
        "basicsr": md.version("basicsr"),
    }
    assert before == after
    import torch
    import torchvision

    assert torch.__version__.startswith(before["torch"].split("+")[0][:4])
    assert torchvision.__version__.startswith(before["torchvision"].split("+")[0][:4])


# ------------------------------------------------------------------
# 10. no site-packages edits
# ------------------------------------------------------------------

def test_no_site_packages_edits():
    import torchvision

    tv_root = Path(torchvision.__file__).resolve().parent
    legacy_file = tv_root / "transforms" / "functional_tensor.py"
    assert not legacy_file.exists(), (
        f"legacy shim must live in sys.modules only, found file {legacy_file}"
    )
    import basicsr

    degrad = Path(basicsr.__file__).resolve().parent / "data" / "degradations.py"
    text = degrad.read_text()
    assert "from torchvision.transforms.functional_tensor import rgb_to_grayscale" in text, (
        "site-packages basicsr must remain unpatched (legacy import line intact)"
    )


# ------------------------------------------------------------------
# 11/12. dependency failure returns clean ToolResult, no dirs created
# ------------------------------------------------------------------

def test_upscale_dependency_failure_returns_toolresult(tmp_path, monkeypatch):
    from tools.enhancement.upscale import Upscale

    # Real tiny input so we pass the input-exists check.
    src = tmp_path / "in.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n")
    out = tmp_path / "newdir" / "out.png"

    monkeypatch.setitem(sys.modules, "basicsr.archs.rrdbnet_arch", None)
    for key in [k for k in list(sys.modules) if k.startswith("basicsr.archs.rrdbnet_arch")]:
        monkeypatch.setitem(sys.modules, key, None)

    tool = Upscale()
    result = tool.execute({"input_path": str(src), "output_path": str(out)})
    assert result.success is False
    assert result.error
    assert "Missing dependency" in result.error or "Dependency" in result.error
    assert not out.exists()
    assert not (tmp_path / "newdir").exists()


def test_face_restore_dependency_failure_returns_toolresult(tmp_path, monkeypatch):
    from tools.enhancement.face_restore import FaceRestore

    src = tmp_path / "in.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n")
    out = tmp_path / "newdir" / "out.png"

    monkeypatch.setitem(sys.modules, "gfpgan", None)

    tool = FaceRestore()
    result = tool.execute({"input_path": str(src), "output_path": str(out)})
    assert result.success is False
    assert result.error
    assert not out.exists()


# ------------------------------------------------------------------
# 13. bg_upsampler=False does not require Real-ESRGAN
# ------------------------------------------------------------------

def test_bg_upsampler_false_does_not_require_realesrgan(monkeypatch):
    from tools.enhancement.face_restore import FaceRestore

    tool = FaceRestore()
    # Base preflight must pass in this env (real deps present).
    assert tool._preflight() is None

    # Hide Real-ESRGAN: bg preflight must now fail, base must still pass.
    saved = {}
    for key in [k for k in list(sys.modules) if k == "realesrgan" or k.startswith("realesrgan.")]:
        saved[key] = sys.modules.pop(key)
    monkeypatch.setitem(sys.modules, "realesrgan", None)
    try:
        assert tool._preflight() is None
        bg_err = tool._preflight_bg_upsampler()
        assert bg_err is not None
        assert "Real-ESRGAN" in bg_err or "realesrgan" in bg_err.lower()
    finally:
        monkeypatch.undo()
        for k, v in saved.items():
            sys.modules[k] = v


# ------------------------------------------------------------------
# 14. bg_upsampler=True does not silently pretend success
# ------------------------------------------------------------------

def test_bg_upsampler_true_fails_explicitly_when_realesrgan_missing(tmp_path, monkeypatch):
    import cv2
    import numpy as np

    from tools.enhancement.face_restore import FaceRestore

    img = np.zeros((32, 32, 3), dtype=np.uint8)
    src = tmp_path / "face.png"
    assert cv2.imwrite(str(src), img)

    monkeypatch.setitem(sys.modules, "realesrgan", None)
    for key in [k for k in list(sys.modules) if k.startswith("realesrgan.")]:
        monkeypatch.setitem(sys.modules, key, None)
    # Also block the basicsr RRDBNet path used only for bg.
    monkeypatch.setitem(sys.modules, "basicsr.archs.rrdbnet_arch", None)

    tool = FaceRestore()
    result = tool.execute({
        "input_path": str(src),
        "output_path": str(tmp_path / "restored.png"),
        "bg_upsampler": True,
    })
    assert result.success is False
    assert "bg_upsampler" in result.error
    # Must not claim an artifact on failure.
    assert result.artifacts == [] or result.artifacts is None or not Path(str(tmp_path / "restored.png")).exists()


# ------------------------------------------------------------------
# 15. fresh-process import path
# ------------------------------------------------------------------

def test_fresh_process_import_path():
    code = (
        "from tools.enhancement._torchvision_compat import ensure_torchvision_compat; "
        "ensure_torchvision_compat(); "
        "from basicsr.archs.rrdbnet_arch import RRDBNet; "
        "from realesrgan import RealESRGANer; "
        "from gfpgan import GFPGANer; "
        "print('FRESH_OK')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr[-2000:]}"
    assert "FRESH_OK" in proc.stdout
    assert "functional_tensor" not in proc.stderr
