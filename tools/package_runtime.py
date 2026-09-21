"""Package a locally built Linux media binary; prints the archive SHA-256."""

import hashlib
import json
import platform
import subprocess
import tarfile
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if platform.system() != "Linux" or platform.machine() != "x86_64":
    raise SystemExit("Build release media archives on Linux x86-64")
subprocess.run(
    [
        "cargo",
        "build",
        "--release",
        "--locked",
        "--manifest-path",
        str(root / "media-core/Cargo.toml"),
    ],
    check=True,
)
output = root / "dist/mba-media-0.2.0-linux-x86_64.tar.gz"
output.parent.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as temp:
    manifest = Path(temp) / "runtime.json"
    manifest.write_text(json.dumps({"version": "0.2.0", "system": "Linux", "machine": "x86_64"}))
    with tarfile.open(output, "w:gz") as tar:
        tar.add(root / "media-core/target/release/mba-media", arcname="mba-media")
        tar.add(manifest, arcname="runtime.json")
with output.open("rb") as source:
    print(hashlib.file_digest(source, "sha256").hexdigest(), output)
