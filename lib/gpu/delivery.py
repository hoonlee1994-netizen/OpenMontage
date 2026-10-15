"""Final-delivery ownership for a rendered video.

A tool that renders a deliverable into a path that already holds an approved
asset must not let any step of the render write that path directly: a render
that fails, is cancelled or is abandoned half-way would leave a truncated file
where the previous valid one was, and a second attempt (a CPU re-render after a
degraded GPU one) would race the first. When a render would overwrite an
existing file, this module gives it one *attempt destination* per attempt -- a
fresh hidden sibling of the final path, same extension, same filesystem -- and
publishes exactly one verified attempt over the final path with the same
all-or-nothing, mode-preserving, rollback-capable replacement the FFmpeg
dispatcher uses (``ffmpeg_exec._commit_outputs``).

Only the owner of the :class:`Delivery` ever publishes; an attempt that outlives
its owner (an orphaned renderer) can only ever write its own, discarded temp.
A destination that does not exist yet has no asset to protect and is written
directly, exactly as before.
"""
from __future__ import annotations

import os
from typing import Optional

from lib.gpu import ffmpeg_args, ffmpeg_exec


class DeliveryError(RuntimeError):
    """The staged output could not be verified or published; the final is untouched."""


def attempt_path(final: str) -> str:
    """A fresh, unique hidden sibling of ``final`` that keeps its extension."""
    d, name = os.path.split(final)
    ext = os.path.splitext(name)[1]
    token = f".om-render-{os.getpid()}-{os.urandom(4).hex()}"
    return os.path.join(d, token + ext) if d else token + ext


def retarget(cmd: list, final: str, staged: str) -> list:
    """``cmd`` with the output ``final`` replaced by ``staged``.

    Recognises the output as a bare token (``remotion render ... out.mp4``) or as
    ``--output=<path>``. Raises ``ValueError`` if the command never names it, so a
    render is never run against an output the owner cannot redirect.
    """
    out, hit = [], 0
    for tok in cmd:
        if tok == final:
            out.append(staged)
            hit += 1
        elif tok.startswith("--output=") and tok[len("--output="):] == final:
            out.append("--output=" + staged)
            hit += 1
        else:
            out.append(tok)
    if not hit:
        raise ValueError(f"render output {final!r} does not appear in the command")
    return out


def _kind(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return "media" if ext in ffmpeg_args._AUDIO_EXT else "video"


class Delivery:
    """Owns one final path for the duration of a render (possibly several attempts).

    ``begin()`` hands out the destination of a new attempt (discarding the
    previous one); ``publish()`` verifies the current attempt and replaces the
    final path with it; ``discard()`` removes whatever is staged. Using the
    object as a context manager discards anything unpublished on exit, so every
    failure, cancellation or exception path leaves the final path untouched.

    With ``only_existing=True`` the delivery stages only when the final path is
    an existing regular file (an asset to protect); otherwise ``staging`` is
    False and every method degrades to a direct write of the final path.
    """

    def __init__(self, final, *, kind: Optional[str] = None, only_existing: bool = False):
        self.requested = str(final)
        self.final = ffmpeg_args.write_target(self.requested)
        self.kind = kind
        self._existed = os.path.isfile(self.final)
        self.staging = (not only_existing) or self._existed
        self.current: Optional[str] = None
        self.attempts = 0

    def begin(self) -> str:
        """The path the next attempt must write (the final path itself if not staging)."""
        self.attempts += 1
        if not self.staging:
            return self.requested
        self.discard()
        self.current = attempt_path(self.final)
        return self.current

    def discard(self) -> None:
        if self.current:
            try:
                os.unlink(self.current)
            except OSError:
                pass
            self.current = None

    def abandon_attempt(self) -> None:
        """Drop the output of an attempt that must never be delivered (failed or degraded).

        A staged attempt is simply discarded. A direct attempt wrote a path that
        did not exist before this render, so what is there is this attempt's own
        flawed output: it is removed rather than left at a production path while
        a redo is still to run (or fails).
        """
        if self.staging:
            self.discard()
            return
        if not self._existed:
            try:
                os.unlink(self.requested)
            except OSError:
                pass

    def verify(self) -> Optional[str]:
        """Why the staged output may not be published, or None.

        A staged file is refused when ffprobe positively shows it is not a
        usable video/media file. An ffprobe that cannot run at all says nothing
        about the render, so it never blocks a file that exists.
        """
        if not self.current or not os.path.isfile(self.current):
            return f"no output was written to {self.current}"
        ffmpeg_exec._load_light()
        try:
            problem = ffmpeg_exec._validate([(self.current, self.kind or _kind(self.final))])
        except Exception as exc:
            problem = f"ffprobe failed on {self.current}: {exc}"
        if problem and problem.startswith("ffprobe failed"):
            return None
        return problem

    def publish(self, *, require_output: bool = True) -> bool:
        """Replace the final path with the verified staged output.

        Returns True when the output was published (or was written directly).
        Raises :class:`DeliveryError` (the final path untouched, the staged file
        discarded) when the output fails verification or the replacement cannot
        be completed. With ``require_output=False`` an attempt that left no
        staged file at all (a renderer that reported success without writing the
        destination it was given) publishes nothing and returns False: it never
        replaces a file with nothing, and the caller's own existence check
        decides what that means.
        """
        if not self.staging:
            return True
        staged = self.current
        if staged is None or (not require_output and not os.path.isfile(staged)):
            self.discard()
            return False
        # A zero-byte file is what a renderer that "succeeded" without producing
        # anything leaves behind; only a non-empty file can be judged as media.
        problem = self.verify() if (require_output or os.path.getsize(staged) > 0) else None
        if problem is None:
            problem = ffmpeg_exec._commit_outputs([(self.final, staged)])
        if problem is not None:
            self.discard()
            raise DeliveryError(f"render output was not published; the existing file is untouched: {problem}")
        self.current = None
        return True

    def __enter__(self) -> "Delivery":
        return self

    def __exit__(self, *exc) -> bool:
        self.discard()
        return False
