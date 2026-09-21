"""Explicit setup only. Importing or starting a session never installs software."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

VERSION = "0.3.0"


def runtime_root():
    return Path(os.environ.get("MBA_RUNTIME_DIR", Path.home() / ".cache/mba" / VERSION))


def install(runtime_dir=None, *, browser=False, media_archive=None, sha256=None):
    root = Path(runtime_dir) if runtime_dir else runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    if browser:
        target = root / "browser"
        target.mkdir(exist_ok=True)
        resource = Path(__file__).parent / "worker"
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(resource / name, target / name)
        subprocess.run(["npm", "ci", "--omit=dev", "--prefix", str(target)], check=True)
        subprocess.run(
            ["node", str(target / "node_modules/playwright/cli.js"), "install", "chromium"],
            env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(root / "browsers")},
            check=True,
        )
    if media_archive:
        archive = Path(media_archive)
        with archive.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if not sha256 or digest != sha256.lower():
            raise ValueError("Supply --sha256 matching the media archive")
        with tempfile.TemporaryDirectory(dir=root) as temp:
            with tarfile.open(archive) as tar:
                members = tar.getmembers()
                if (
                    len(members) != 2
                    or {m.name for m in members} != {"mba-media", "runtime.json"}
                    or any(not m.isfile() for m in members)
                ):
                    raise ValueError("Unexpected files in runtime archive")
                tar.extractall(temp, filter="data")
            info = json.loads((Path(temp) / "runtime.json").read_text())
            if info != {
                "version": VERSION,
                "system": platform.system(),
                "machine": platform.machine(),
            }:
                raise ValueError("Runtime version or platform mismatch")
            binary = root / "mba-media"
            shutil.copyfile(Path(temp) / "mba-media", binary)
            binary.chmod(0o755)
    return root
