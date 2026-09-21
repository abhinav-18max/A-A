"""Run native audio/display acceptance in Docker, including Docker Desktop on Mac."""

import argparse
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--image", default="mba-worker:0.3.0-arm64")
parser.add_argument("--script", type=Path, default=Path("tools/container_smoke.py"))
args = parser.parse_args()
repo = Path(__file__).resolve().parents[1]
artifacts = repo / ".cache/container-artifacts"
artifacts.mkdir(parents=True, exist_ok=True)
artifacts.chmod(0o777)  # Disposable test evidence, writable by the container's nobody user.
subprocess.run(
    [
        "docker",
        "run",
        "--rm",
        "--init",
        "--user",
        "65534:65534",
        "--shm-size=1g",
        "--cap-drop=ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--security-opt",
        f"seccomp={repo / 'deploy/seccomp.json'}",
        "--env",
        "HOME=/tmp/mba-home",
        "--env",
        "MBA_SMOKE_ROOT=/artifacts",
        "--mount",
        f"type=bind,src={args.script.resolve()},dst=/test.py,readonly",
        "--mount",
        f"type=bind,src={artifacts},dst=/artifacts",
        "--entrypoint",
        "/bin/sh",
        args.image,
        "-c",
        "mkdir -p /tmp/mba-home && exec python /test.py",
    ],
    check=True,
)
