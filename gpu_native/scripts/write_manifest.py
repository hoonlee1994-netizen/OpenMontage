#!/usr/bin/env python3
"""Record what the GPU runtime was built from (for provenance and probes)."""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    home, debs = Path(argv[1]), Path(argv[2])
    packages = {}
    for name in argv[3:]:
        matches = sorted(debs.glob(f"{name}_*.deb"))
        if matches:
            deb = matches[0]
            packages[name] = {
                "file": deb.name,
                "sha256": hashlib.sha256(deb.read_bytes()).hexdigest(),
            }
    manifest = {
        "schema": 1,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "home": str(home),
        "packages": packages,
    }
    (home / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
