"""Final-delivery ownership of renders (L5 full-scope finding F1-c).

A render that is about to overwrite an approved deliverable renders every
attempt into its own staged sibling and publishes exactly one verified attempt.
Failed, cancelled, degraded-then-redone and orphaned attempts can therefore
never leave a truncated or superseded file where the approved one was.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from lib.gpu import ffmpeg_exec, remotion, runtime, telemetry
from lib.gpu.delivery import Delivery, DeliveryError, attempt_path, retarget

REAL_VALIDATE = ffmpeg_exec._validate

NEEDS_FFPROBE = pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not available")


def names(d):
    return sorted(p.name for p in Path(d).iterdir())


@pytest.fixture(autouse=True)
def stand_in_media(monkeypatch):
    """The fake renderers write opaque bytes: media verification is exercised by the tests that ask for it."""
    monkeypatch.setattr(ffmpeg_exec, "_validate", lambda outputs: None)


@pytest.fixture
def real_ffprobe(monkeypatch):
    monkeypatch.setattr(ffmpeg_exec, "_validate", REAL_VALIDATE)


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------

def test_a_new_destination_is_written_directly(tmp_path):
    final = tmp_path / "new.mp4"
    d = Delivery(final, only_existing=True)
    assert d.staging is False
    assert d.begin() == str(final)
    final.write_bytes(b"render")
    assert d.publish() is True and final.read_bytes() == b"render"
    d.discard()
    assert names(tmp_path) == ["new.mp4"]


def test_an_existing_file_is_staged_in_a_hidden_sibling_with_the_same_extension(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    d = Delivery(final, only_existing=True)
    staged = d.begin()
    assert d.staging and staged != str(final)
    assert os.path.dirname(staged) == str(tmp_path) and os.path.basename(staged).startswith(".om-render-")
    assert staged.endswith(".mp4")


def test_every_attempt_has_its_own_destination_and_the_previous_one_is_discarded(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    d = Delivery(final, only_existing=True)
    first = d.begin()
    Path(first).write_bytes(b"gpu attempt")
    second = d.begin()
    assert first != second and not os.path.exists(first)
    assert final.read_bytes() == b"APPROVED"


def test_publish_replaces_the_final_atomically_and_keeps_its_mode(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    os.chmod(final, 0o640)
    d = Delivery(final, only_existing=True)
    Path(d.begin()).write_bytes(b"NEW")
    assert d.publish(require_output=False) is True
    assert final.read_bytes() == b"NEW" and stat.S_IMODE(os.stat(final).st_mode) == 0o640
    assert names(tmp_path) == ["approved.mp4"]


def test_context_exit_discards_an_unpublished_attempt(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    with Delivery(final, only_existing=True) as d:
        Path(d.begin()).write_bytes(b"half a render")
    assert final.read_bytes() == b"APPROVED" and names(tmp_path) == ["approved.mp4"]


def test_a_symlinked_deliverable_is_replaced_through_its_link(tmp_path):
    real = tmp_path / "real.mp4"
    real.write_bytes(b"APPROVED")
    link = tmp_path / "link.mp4"
    link.symlink_to(real)
    d = Delivery(link, only_existing=True)
    staged = d.begin()
    assert os.path.dirname(staged) == str(tmp_path)
    Path(staged).write_bytes(b"NEW")
    d.publish(require_output=False)
    assert link.is_symlink() and real.read_bytes() == b"NEW"


def test_an_attempt_that_wrote_nothing_publishes_nothing(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    d = Delivery(final, only_existing=True)
    d.begin()
    assert d.publish(require_output=False) is False
    assert final.read_bytes() == b"APPROVED"
    with pytest.raises(DeliveryError):
        d2 = Delivery(final, only_existing=True)
        d2.begin()
        d2.publish(require_output=True)
    assert final.read_bytes() == b"APPROVED"


@NEEDS_FFPROBE
def test_an_output_that_is_not_media_is_refused_and_the_approved_asset_survives(tmp_path, real_ffprobe):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    d = Delivery(final, only_existing=True)
    Path(d.begin()).write_bytes(b"this is not a video at all")
    with pytest.raises(DeliveryError):
        d.publish(require_output=False)
    assert final.read_bytes() == b"APPROVED" and names(tmp_path) == ["approved.mp4"]


def test_a_directory_in_the_way_is_refused_before_anything_changes(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    d = Delivery(final, only_existing=True)
    Path(d.begin()).write_bytes(b"x")
    shutil.rmtree(tmp_path / "nothing", ignore_errors=True)
    final.unlink()
    final.mkdir()           # the destination became a directory meanwhile
    with pytest.raises(DeliveryError):
        d.publish(require_output=False)
    assert final.is_dir()


def test_retarget_replaces_a_positional_output_and_an_output_option():
    cmd = ["npx", "remotion", "render", "entry.tsx", "Comp", "/o/out.mp4", "--props=/p.json"]
    assert retarget(cmd, "/o/out.mp4", "/o/.s.mp4")[5] == "/o/.s.mp4"
    cmd = ["npx", "remotion", "render", "TalkingHead", "--output=/o/out.mp4"]
    assert retarget(cmd, "/o/out.mp4", "/o/.s.mp4")[-1] == "--output=/o/.s.mp4"
    with pytest.raises(ValueError):
        retarget(["npx", "remotion", "render"], "/o/out.mp4", "/o/.s.mp4")


def test_attempt_paths_are_unique():
    assert attempt_path("/o/out.mp4") != attempt_path("/o/out.mp4")


# --------------------------------------------------------------------------
# run_render: the render owns its deliverable
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    state = tmp_path.parent / f"{tmp_path.name}-state"        # outside the directory the tests inspect
    monkeypatch.setenv("OM_GPU_HOME", str(state / "gpu-home"))
    monkeypatch.setattr(telemetry, "record", lambda ev: None)
    monkeypatch.setattr(remotion, "binaries_dir", lambda: state / "remotion-bin")
    monkeypatch.setattr(remotion, "cpu_browser", lambda: "/usr/bin/chromium")
    monkeypatch.setenv("OM_GPU_MODE", "auto")
    monkeypatch.setattr(remotion, "plan", lambda: {"gpu": True, "renderer": "mali"})


class Watch:
    verdict_value = ("gpu", [])

    def __init__(self, render_id):
        self.seen = {"1": ["--use-angle=gles-egl", "--use-gl=angle"]}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def verdict(self):
        return Watch.verdict_value


@pytest.fixture(autouse=True)
def fake_watch(monkeypatch):
    monkeypatch.setattr(remotion, "_GpuProcessWatch", Watch)
    Watch.verdict_value = ("gpu", [])
    yield
    Watch.verdict_value = ("gpu", [])


def render_cmd(final):
    return ["npx", "remotion", "render", "src/index.tsx", "Explainer", str(final), "--props=/p.json"]


def output_of(cmd, final):
    """The path a command renders to: the staged path, or the final one when not staged."""
    return next(c for c in cmd if c.endswith(".mp4"))


class Runner:
    """A renderer: writes whatever ``payload`` says to the path in its command."""

    def __init__(self, final, *, gpu_payload=b"GPU", cpu_payload=b"CPU", fail_gpu=False, fail_cpu=False,
                 exc_gpu=None, seen_final=None):
        self.final = Path(final)
        self.gpu_payload, self.cpu_payload = gpu_payload, cpu_payload
        self.fail_gpu, self.fail_cpu, self.exc_gpu = fail_gpu, fail_cpu, exc_gpu
        self.calls, self.finals_seen, self.targets = [], [], []

    def __call__(self, cmd, timeout=None, cwd=None):
        self.calls.append(cmd)
        target = output_of(cmd, self.final)
        self.targets.append(target)
        self.finals_seen.append(self.final.read_bytes() if self.final.exists() else None)
        gpu = any(str(runtime.chromium_launcher()) in c for c in cmd)
        if gpu and self.exc_gpu:
            raise self.exc_gpu
        Path(target).write_bytes(self.gpu_payload if gpu else self.cpu_payload)
        if (gpu and self.fail_gpu) or (not gpu and self.fail_cpu):
            raise subprocess.CalledProcessError(1, cmd, stderr="boom")


def test_a_render_over_an_existing_file_renders_to_a_staged_path_and_publishes_it(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    run = Runner(final)
    rec = remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=run, output=final)
    assert run.targets[0] != str(final) and os.path.basename(run.targets[0]).startswith(".om-render-")
    assert run.finals_seen == [b"APPROVED"]            # untouched while the renderer ran
    assert final.read_bytes() == b"GPU" and rec["backend"] == "gpu"
    assert rec["delivery"] == {"mode": "staged", "attempts": 1, "published": True}
    assert names(tmp_path) == ["approved.mp4"]


def test_a_render_to_a_new_path_is_not_redirected(tmp_path):
    final = tmp_path / "new.mp4"
    run = Runner(final)
    rec = remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=run, output=final)
    assert run.targets == [str(final)] and final.read_bytes() == b"GPU"
    assert rec["delivery"]["mode"] == "direct"


def test_without_an_owned_output_the_command_is_unchanged(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    run = Runner(final)
    remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=run)
    assert run.targets == [str(final)] and final.read_bytes() == b"GPU"


def test_a_failed_gpu_render_keeps_the_approved_asset_while_the_cpu_render_runs(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    run = Runner(final, fail_gpu=True)
    rec = remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=run, output=final)
    assert rec["backend"] == "cpu" and len(run.calls) == 2
    assert run.targets[0] != run.targets[1]            # an independent destination per attempt
    assert run.finals_seen == [b"APPROVED", b"APPROVED"]
    assert final.read_bytes() == b"CPU" and names(tmp_path) == ["approved.mp4"]


def test_a_degraded_gpu_render_is_never_published_even_if_the_cpu_redo_then_fails(tmp_path):
    """The gap between a degraded GPU attempt and its CPU redo must not hold a superseded asset."""
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    Watch.verdict_value = ("gpu-degraded", ["--use-gl=disabled"])
    run = Runner(final, fail_cpu=True)
    with pytest.raises(subprocess.CalledProcessError):
        remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=run, output=final)
    assert len(run.calls) == 2
    assert run.finals_seen == [b"APPROVED", b"APPROVED"]   # the CPU redo started with the asset intact
    assert final.read_bytes() == b"APPROVED" and names(tmp_path) == ["approved.mp4"]


def test_a_degraded_gpu_render_is_replaced_by_the_cpu_render(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    Watch.verdict_value = ("gpu-degraded", ["--use-gl=disabled"])
    run = Runner(final)
    rec = remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=run, output=final)
    assert rec["backend"] == "cpu" and final.read_bytes() == b"CPU"
    assert rec["delivery"]["attempts"] == 2


def test_require_mode_failure_keeps_the_approved_asset(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_GPU_MODE", "require")
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    run = Runner(final, fail_gpu=True)
    with pytest.raises(subprocess.CalledProcessError):
        remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=run, output=final)
    assert len(run.calls) == 1 and final.read_bytes() == b"APPROVED" and names(tmp_path) == ["approved.mp4"]


def test_a_timeout_keeps_the_approved_asset(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    run = Runner(final, exc_gpu=subprocess.TimeoutExpired("npx", 10))
    with pytest.raises(subprocess.TimeoutExpired):
        remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=run, output=final)
    assert final.read_bytes() == b"APPROVED" and names(tmp_path) == ["approved.mp4"]


def test_a_cancellation_keeps_the_approved_asset(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    run = Runner(final, exc_gpu=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=run, output=final)
    assert final.read_bytes() == b"APPROVED" and names(tmp_path) == ["approved.mp4"]


def test_an_orphaned_renderer_can_only_write_its_own_discarded_attempt(tmp_path):
    """After the owner moved on (fallback / cancellation) a late writer never reaches the deliverable."""
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    Watch.verdict_value = ("gpu-degraded", ["--use-gl=disabled"])
    run = Runner(final)
    remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=run, output=final)
    gpu_attempt_path = run.targets[0]            # the abandoned GPU attempt's destination
    assert final.read_bytes() == b"CPU"
    Path(gpu_attempt_path).write_bytes(b"LATE ORPHAN WRITE")   # the orphan finally writes
    assert final.read_bytes() == b"CPU"


@NEEDS_FFPROBE
def test_a_render_that_is_not_media_is_not_published(tmp_path, real_ffprobe):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    run = Runner(final, gpu_payload=b"definitely not an mp4")
    with pytest.raises(DeliveryError):
        remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=run, output=final)
    assert final.read_bytes() == b"APPROVED" and names(tmp_path) == ["approved.mp4"]


def test_a_renderer_that_ignores_its_staged_destination_replaces_nothing(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")

    def rogue(cmd, timeout=None, cwd=None):
        return None                                  # reports success, writes nothing

    rec = remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=rogue, output=final)
    assert rec["delivery"]["published"] is False and "wrote nothing" in rec["warning"]
    assert final.read_bytes() == b"APPROVED" and names(tmp_path) == ["approved.mp4"]


def test_the_output_option_form_is_redirected_too(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    cmd = ["npx", "remotion", "render", "TalkingHead", "--codec=h264", f"--output={final}"]
    seen = {}

    def run(c, timeout=None, cwd=None):
        out = next(x for x in c if x.startswith("--output="))[len("--output="):]
        seen["out"] = out
        Path(out).write_bytes(b"NEW")

    remotion.run_render(cmd, cwd=None, timeout=10, runner=run, output=final)
    assert seen["out"] != str(final) and final.read_bytes() == b"NEW"


# --------------------------------------------------------------------------
# finalize: a further step of the same delivery (e.g. the audio mux)
# --------------------------------------------------------------------------

def test_a_finalize_step_runs_on_the_staged_render_before_it_is_published(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    run = Runner(final)
    seen = {}

    def finalize(path):
        seen["path"] = path
        seen["final_during"] = final.read_bytes()
        Path(path).write_bytes(Path(path).read_bytes() + b"+AUDIO")

    remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=run, output=final, finalize=finalize)
    assert seen["path"] != str(final) and seen["final_during"] == b"APPROVED"
    assert final.read_bytes() == b"GPU+AUDIO" and names(tmp_path) == ["approved.mp4"]


def test_a_failing_finalize_step_keeps_the_approved_asset(tmp_path):
    final = tmp_path / "approved.mp4"
    final.write_bytes(b"APPROVED")
    run = Runner(final)

    def finalize(path):
        raise RuntimeError("audio mux failed")

    with pytest.raises(RuntimeError, match="audio mux failed"):
        remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=run, output=final, finalize=finalize)
    assert final.read_bytes() == b"APPROVED" and names(tmp_path) == ["approved.mp4"]


def test_finalize_runs_on_the_final_path_when_nothing_needs_protecting(tmp_path):
    final = tmp_path / "new.mp4"
    run = Runner(final)
    seen = []
    remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=run, output=final,
                        finalize=lambda path: seen.append(path))
    assert seen == [str(final)]


def test_finalize_needs_an_owned_output(tmp_path):
    with pytest.raises(ValueError):
        remotion.run_render(render_cmd(tmp_path / "x.mp4"), cwd=None, timeout=10, runner=Runner(tmp_path / "x.mp4"),
                            finalize=lambda path: None)


def test_a_degraded_render_to_a_new_path_does_not_leave_its_flawed_output_when_the_redo_fails(tmp_path):
    final = tmp_path / "new.mp4"
    Watch.verdict_value = ("gpu-degraded", ["--use-gl=disabled"])
    seen = []

    def run(cmd, timeout=None, cwd=None):
        target = Path(output_of(cmd, final))
        if any(str(runtime.chromium_launcher()) in c for c in cmd):
            target.write_bytes(b"DEGRADED GPU FRAMES")
            return
        seen.append(target.exists())              # the redo starts from a clean path
        raise subprocess.CalledProcessError(1, cmd, stderr="boom")

    with pytest.raises(subprocess.CalledProcessError):
        remotion.run_render(render_cmd(final), cwd=None, timeout=10, runner=run, output=final)
    assert seen == [False] and not final.exists()  # the degraded frames are not left at the production path
