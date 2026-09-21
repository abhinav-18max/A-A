"""Resolve and record an immutable base, then build the Linux worker image."""

import argparse
import json
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--tag", default="mba-worker:0.3.0")
parser.add_argument("--base", default="ubuntu:24.04")
parser.add_argument("--platform", choices=["linux/amd64", "linux/arm64"], default="linux/amd64")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
subprocess.run(["docker", "pull", "--platform", args.platform, args.base], check=True)
base = json.loads(subprocess.check_output(["docker", "image", "inspect", args.base]))[0][
    "RepoDigests"
][0]
subprocess.run(["docker", "pull", "--platform", args.platform, "node:22-bookworm-slim"], check=True)
node = json.loads(subprocess.check_output(["docker", "image", "inspect", "node:22-bookworm-slim"]))[
    0
]["RepoDigests"][0]
subprocess.run(
    [
        "docker",
        "build",
        "--platform",
        args.platform,
        "--build-arg",
        f"BASE_IMAGE={base}",
        "--build-arg",
        f"NODE_IMAGE={node}",
        "--file",
        "deploy/Dockerfile",
        "--tag",
        args.tag,
        ".",
    ],
    cwd=root,
    check=True,
)
image = json.loads(subprocess.check_output(["docker", "image", "inspect", args.tag]))[0]
(root / "deploy" / "build-manifest.json").write_text(
    json.dumps(
        {
            "base": base,
            "node": node,
            "image_id": image["Id"],
            "tag": args.tag,
            "platform": args.platform,
            "created": image["Created"],
            "note": "OS package versions are recorded in /opt/mba/os-packages.txt",
        },
        indent=2,
    )
)
print(image["Id"])
