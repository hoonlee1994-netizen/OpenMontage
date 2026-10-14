"""Contract tests for the PRoot-free execution channel (lib/gpu/oop).

The channel exists to run the native (bionic) FFmpeg outside PRoot's syscall
tracing.  What must hold no matter what the device looks like:

* guest paths are mapped to host paths exactly as PRoot would, because the two
  namespaces differ (PRoot's ``/`` is a directory on the host);
* an argument that cannot be mapped is passed through unchanged, so a wrong
  guess degrades to the traced path instead of to a wrong file;
* when the worker is unavailable or disabled, every entry point reports that
  and the caller keeps its own fallback.
"""
from __future__ import annotations

import os

from lib.gpu import oop

ROOTFS = "/data/…/debian/rootfs"
BINDS = [
    ("/dev", "/dev"),
    ("/data/data/com.termux/files/home", "/data/data/com.termux/files/home"),
    ("/storage/self/primary", "/sdcard"),
]


def ns():
    return oop.Namespace(ROOTFS, list(BINDS))


def test_identity_bind_is_not_rewritten():
    assert ns().to_host("/dev/null") == "/dev/null"
    assert ns().to_host("/data/data/com.termux/files/home/a.mp4") == \
        "/data/data/com.termux/files/home/a.mp4"


def test_guest_path_is_prefixed_with_the_rootfs():
    assert ns().to_host("/root/projects/x.mp4") == ROOTFS + "/root/projects/x.mp4"


def test_bind_with_a_different_guest_name_uses_the_host_side():
    assert ns().to_host("/sdcard/Download/a.mp4") == "/storage/self/primary/Download/a.mp4"


def test_host_path_that_is_already_host_side_is_untouched():
    assert ns().to_host(ROOTFS + "/root/x") == ROOTFS + "/root/x"


def test_relative_paths_are_never_rewritten():
    assert ns().to_host("out/job0.mp4") == "out/job0.mp4"


def test_longest_guest_prefix_wins():
    n = oop.Namespace(ROOTFS, [("/host/a", "/a"), ("/host/ab", "/a/b")])
    assert n.to_host("/a/b/c") == "/host/ab/c"
    assert n.to_host("/a/d") == "/host/a/d"


def test_embedded_filter_path_is_rewritten_when_the_host_file_exists(tmp_path):
    root = tmp_path / "rootfs"
    (root / "root" / "media").mkdir(parents=True)
    srt = root / "root" / "media" / "sub.srt"
    srt.write_text("1\n")
    n = oop.Namespace(str(root), [])
    out = n.to_host_any("subtitles='/root/media/sub.srt':force_style='Font=x'")
    assert str(srt) in out
    assert "force_style='Font=x'" in out


def test_embedded_filter_path_is_left_alone_when_the_target_is_missing(tmp_path):
    n = oop.Namespace(str(tmp_path / "rootfs"), [])
    arg = "subtitles='/root/media/missing.srt'"
    assert n.to_host_any(arg) == arg


def test_plain_filter_graph_is_not_touched():
    for arg in ("scale=1920:1080,setsar=1,fps=30", "-vf", "yuv420p"):
        assert ns().to_host_any(arg) == arg


def test_parse_binds_and_rootfs_from_a_real_pr_root_command_line():
    argv = ["proot", "--kill-on-exit", "--rootfs=/r/rootfs", "--bind=/dev",
            "--bind=/storage/self/primary:/sdcard", "--bind=/data/x", "/bin/bash", "-l"]
    rootfs, binds = oop._binds_from_cmdline(argv)
    assert rootfs == "/r/rootfs"
    assert ("/dev", "/dev") in binds
    assert ("/storage/self/primary", "/sdcard") in binds
    assert ("/data/x", "/data/x") in binds


def test_disabled_worker_never_runs(monkeypatch):
    monkeypatch.setenv("OM_GPU_OOP", "0")
    assert oop.ensure_worker(spawn=True) is False
    assert oop.run(["echo", "hi"]) is None


def test_run_without_spawning_returns_none_when_no_worker_is_live(monkeypatch, tmp_path):
    monkeypatch.setenv("OM_GPU_OOP", "auto")
    monkeypatch.setenv("OM_GPU_HOME", str(tmp_path / "home"))
    assert oop.ensure_worker(spawn=False) is False
    assert oop.run(["echo", "hi"], wait_s=0.0) is None


def test_worker_script_and_socket_live_under_home(monkeypatch, tmp_path):
    monkeypatch.setenv("OM_GPU_HOME", str(tmp_path / "home"))
    assert oop.socket_path().parent.parent.name == "home"
    assert oop.worker_script().name == "om_gpu_worker.py"


def test_spawn_uses_a_host_path_for_the_worker_script(monkeypatch):
    """The Termux app forks the worker outside PRoot: it cannot see /root/…."""
    captured = {}
    monkeypatch.setattr(oop, "_run_dir", lambda: __import__("pathlib").Path("/data/data/com.termux/files/home/.openmontage-gpu/run"))

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(oop.subprocess, "run", fake_run)
    monkeypatch.setattr(oop, "namespace", lambda: oop.Namespace("/ROOTFS", []))
    monkeypatch.setattr(oop, "worker_script",
                        lambda: __import__("pathlib").Path("/root/projects/openmontage/gpu_native/worker/om_gpu_worker.py"))
    oop._startservice()
    args = captured["cmd"]
    assert args[0] == "am"
    joined = " ".join(args)
    assert "/ROOTFS/root/projects/openmontage/gpu_native/worker/om_gpu_worker.py" in joined
    assert "--sock" in joined and "--ready" in joined
    assert os.path.sep.join(["com.termux", "com.termux.app.TermuxService"]) in joined
