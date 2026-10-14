"""Entry point for the PATH ffmpeg shim (runs with ``python3 -I -S``).

Kept import-free beyond os/sys: it runs for every ffmpeg call.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.gpu.ffmpeg_exec import main  # noqa: E402

sys.exit(main())
