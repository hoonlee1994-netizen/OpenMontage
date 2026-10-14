#!/usr/bin/env python3
"""Fetch pinned Termux .deb packages into the project-scoped GPU runtime.

Termux's apt refuses to run as (proot-faked) root, so packages are fetched by
their index ``Filename`` and verified against the SHA256 recorded in the
apt-verified Packages index. Nothing is installed into the global Termux prefix.
"""
from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

LISTS = Path("/data/data/com.termux/files/usr/var/lib/apt/lists")
REPOS = {
    "termux.net_dists_stable_main_binary-aarch64_Packages": "https://termux.net/",
    "x11-packages.termux.net_dists_x11_main_binary-aarch64_Packages": "https://x11-packages.termux.net/",
}


def parse_index() -> dict[str, dict[str, str]]:
    pkgs: dict[str, dict[str, str]] = {}
    for fname, base in REPOS.items():
        path = LISTS / fname
        if not path.exists():
            continue
        for block in path.read_text(encoding="utf-8", errors="replace").split("\n\n"):
            fields: dict[str, str] = {}
            for line in block.splitlines():
                if ":" in line and not line.startswith(" "):
                    k, v = line.split(":", 1)
                    fields[k.strip()] = v.strip()
            if "Package" in fields and "Filename" in fields:
                fields["_base"] = base
                pkgs.setdefault(fields["Package"], fields)
    return pkgs


def main(argv: list[str]) -> int:
    out = Path(argv[1])
    names = argv[2:]
    out.mkdir(parents=True, exist_ok=True)
    index = parse_index()
    failed = []
    for name in names:
        meta = index.get(name)
        if meta is None:
            print(f"MISSING-IN-INDEX {name}")
            failed.append(name)
            continue
        dest = out / Path(meta["Filename"]).name
        want = meta.get("SHA256", "")
        if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == want:
            print(f"cached {dest.name}")
            continue
        url = meta["_base"] + meta["Filename"]
        tmp = dest.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as fh:
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
        got = hashlib.sha256(tmp.read_bytes()).hexdigest()
        if got != want:
            tmp.unlink()
            print(f"SHA256-MISMATCH {name} {got} != {want}")
            failed.append(name)
            continue
        tmp.replace(dest)
        print(f"ok {dest.name} {meta.get('Version')}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
