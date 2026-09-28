"""Narrow torchvision compatibility bridge for BasicSR / Real-ESRGAN / GFPGAN.

BasicSR 1.4.2 imports ``rgb_to_grayscale`` from the long-removed legacy
module ``torchvision.transforms.functional_tensor``. Modern torchvision
(>=0.15, verified with 0.29) still ships the same symbol at
``torchvision.transforms.functional.rgb_to_grayscale``.

This helper installs the minimum ``sys.modules`` alias required so the
unmodified third-party stack imports without touching site-packages or
downgrading torch/torchvision. It fills exactly one removed symbol.

Contract:
  - legacy module importable          -> no-op, return False
  - legacy missing, modern present    -> install alias, return True
  - legacy already aliased by us      -> idempotent no-op, return True
  - torchvision itself missing        -> raise ImportError (truthful)
  - modern replacement missing        -> raise ImportError (truthful)
  - unrelated import/runtime error    -> propagate, never swallowed
"""

from __future__ import annotations

import importlib
import sys
import types

LEGACY_MODULE = "torchvision.transforms.functional_tensor"
MODERN_MODULE = "torchvision.transforms.functional"
SYMBOL = "rgb_to_grayscale"

_COMPAT_MARKER = "__openmontage_compat__"


def ensure_torchvision_compat() -> bool:
    """Ensure ``torchvision.transforms.functional_tensor`` is importable.

    Returns:
        False when the legacy module already exists natively (no-op).
        True when the narrow OpenMontage alias is active (just installed
        or installed by an earlier idempotent call).

    Raises:
        ImportError: torchvision missing, or the modern replacement
            symbol is missing.
    """
    existing = sys.modules.get(LEGACY_MODULE)
    if existing is not None:
        if getattr(existing, _COMPAT_MARKER, False):
            return True
        # Some other module already occupies the name (native legacy or a
        # test double). Do not touch it.
        return False

    try:
        mod = importlib.import_module(LEGACY_MODULE)
    except ModuleNotFoundError as exc:
        # Only handle the case where the legacy module itself is absent.
        # Anything else (e.g. torchvision missing, torch missing inside
        # torchvision) must fail truthfully, not be masked by an alias.
        if exc.name != LEGACY_MODULE:
            raise ImportError(
                f"torchvision compatibility unavailable: {exc}"
            ) from exc
        # Fall through to alias installation below.
    except ImportError:
        # Legacy module exists but raises ImportError internally: a deeper
        # incompatibility, not the narrow removed-module case. Propagate.
        raise
    else:
        # Native legacy module present.
        return False

    # Legacy module absent: torchvision itself must still be importable.
    try:
        importlib.import_module("torchvision")
    except (ImportError, ModuleNotFoundError) as exc:
        raise ImportError(f"torchvision is not installed: {exc}") from exc

    # Modern replacement must exist.
    try:
        modern = importlib.import_module(MODERN_MODULE)
    except (ImportError, ModuleNotFoundError) as exc:
        raise ImportError(
            f"torchvision is installed but {MODERN_MODULE} is unavailable: {exc}"
        ) from exc

    try:
        symbol = getattr(modern, SYMBOL)
    except AttributeError as exc:
        raise ImportError(
            f"torchvision is installed but {MODERN_MODULE}.{SYMBOL} "
            f"is missing; cannot bridge {LEGACY_MODULE}"
        ) from exc

    shim = types.ModuleType(LEGACY_MODULE)
    shim.__doc__ = (
        "OpenMontage narrow compatibility shim: re-exports "
        f"{MODERN_MODULE}.{SYMBOL} for BasicSR 1.4.2. "
        "Only this symbol is bridged; nothing else is patched."
    )
    setattr(shim, SYMBOL, symbol)
    setattr(shim, _COMPAT_MARKER, True)
    sys.modules[LEGACY_MODULE] = shim
    return True
