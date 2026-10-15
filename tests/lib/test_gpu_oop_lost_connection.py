"""A dropped worker connection after the request was sent must not look like "nothing ran".

L5 sub-report (runtime): ``oop._call`` returned None both when no worker was
reachable (nothing sent) and when the connection died after the request may
have started the encoder; the dispatcher then reused the same staged output for
its fallback while the first writer might still be running.
"""
from __future__ import annotations

import struct

from lib.gpu import ffmpeg_exec, oop, runtime


class _Sock:
    """A worker connection that accepts the request and then goes away."""

    def __init__(self, reply=b""):
        self.sent, self.reply, self.closed = b"", reply, False

    def settimeout(self, t):
        pass

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        chunk, self.reply = self.reply[:n], self.reply[n:]
        return chunk

    def close(self):
        self.closed = True


def test_a_connection_lost_before_any_reply_is_reported_as_possibly_started(monkeypatch):
    sock = _Sock()
    monkeypatch.setattr(oop, "_connect", lambda timeout: sock)
    result = oop._call({"argv": ["ffmpeg"]}, 5)
    assert result is not None and result.error and "may have started" in result.error
    assert sock.sent and sock.closed                      # the request really went out


def test_a_connection_lost_mid_reply_is_reported_as_possibly_started(monkeypatch):
    sock = _Sock(struct.pack(">I", 100) + b"{")           # promises 100 bytes, delivers 1
    monkeypatch.setattr(oop, "_connect", lambda timeout: sock)
    result = oop._call({"argv": ["ffmpeg"]}, 5)
    assert result is not None and result.error


def test_no_worker_at_all_still_means_nothing_was_sent(monkeypatch):
    monkeypatch.setattr(oop, "_connect", lambda timeout: None)
    assert oop._call({"argv": ["ffmpeg"]}, 5) is None


def test_the_dispatcher_restages_when_the_connection_was_lost_after_sending(monkeypatch):
    ffmpeg_exec._load()
    monkeypatch.setattr(oop, "run", lambda *a, **k: oop._lost())
    monkeypatch.setattr(runtime, "native_env", lambda: {})
    attempted, result = ffmpeg_exec._run_native_remote(["/native/ffmpeg", "-i", "in.mp4", "/o/.t.mp4"],
                                                       capture_stderr=True)
    assert attempted is True and result is None           # a run may have started: use a fresh temp


def test_the_dispatcher_reuses_its_temp_when_no_worker_was_reachable(monkeypatch):
    ffmpeg_exec._load()
    monkeypatch.setattr(oop, "run", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "native_env", lambda: {})
    attempted, result = ffmpeg_exec._run_native_remote(["/native/ffmpeg", "-i", "in.mp4", "/o/.t.mp4"],
                                                       capture_stderr=True)
    assert attempted is False and result is None
