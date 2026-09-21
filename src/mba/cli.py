from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path


def preflight(args):
    checks = {"linux": platform.system() == "Linux", "docker": bool(shutil.which("docker"))}
    for camera in args.cameras.split(","):
        checks[camera] = Path(camera).exists() and os.access(camera, os.R_OK | os.W_OK)
    checks["seccomp"] = Path(args.seccomp).is_file()
    if checks["docker"]:
        checks["daemon"] = subprocess.run(["docker", "info"], capture_output=True).returncode == 0
        checks["image"] = (
            subprocess.run(
                ["docker", "image", "inspect", args.image], capture_output=True
            ).returncode
            == 0
        )
    print(json.dumps({"ok": all(checks.values()), "checks": checks}, indent=2))
    return 0 if all(checks.values()) else 1


def main():
    parser = argparse.ArgumentParser(prog="mba")
    sub = parser.add_subparsers(dest="command", required=True)
    mcp = sub.add_parser("mcp")
    mcp.add_argument("--transport", choices=["stdio"], default="stdio")
    mcp.add_argument("--worker-url")
    worker = sub.add_parser("worker")
    worker.add_argument("--root", type=Path, default=Path("/artifacts"))
    worker.add_argument("--host", default="0.0.0.0")
    worker.add_argument("--port", type=int, default=8080)
    for name in ("serve", "preflight"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, default=Path("artifacts"))
        command.add_argument("--image", default="mba-worker:0.3.0")
        command.add_argument("--cameras", default="/dev/video10,/dev/video11")
        command.add_argument("--seccomp", default="deploy/seccomp.json")
        command.add_argument("--host", default="127.0.0.1")
        command.add_argument("--port", type=int, default=8090)
    setup = sub.add_parser("setup")
    setup.add_argument("--runtime-dir", type=Path)
    setup.add_argument("--browser", action="store_true")
    setup.add_argument("--media-archive", type=Path)
    setup.add_argument("--sha256")
    args = parser.parse_args()
    if args.command == "mcp":
        import asyncio

        from mba.mcp_server import run

        asyncio.run(run(args.worker_url, os.environ.get("MBA_TOKEN", "")))
        return
    if args.command == "setup":
        from mba.setup import install

        if not args.browser and not args.media_archive:
            parser.error("choose --browser or --media-archive")
        print(
            install(
                args.runtime_dir,
                browser=args.browser,
                media_archive=args.media_archive,
                sha256=args.sha256,
            )
        )
        return
    if args.command == "preflight":
        raise SystemExit(preflight(args))
    token = os.environ.get("MBA_TOKEN", "")
    if len(token) < 24:
        parser.error("set MBA_TOKEN to a random token of at least 24 characters")
    import uvicorn

    if args.command == "worker":
        Path(os.environ.get("HOME", "/tmp/mba-home")).mkdir(parents=True, exist_ok=True)
        from mba.service import create_worker_app

        app = create_worker_app(args.root, token, session_id=os.environ.get("MBA_SESSION_ID"))
    else:
        from mba.controller import Controller, DockerHost, create_controller_app

        if os.getuid() == 0:
            parser.error("run the controller as a non-root Docker-authorized user")
        host = DockerHost(args.root, args.image, Path(args.seccomp))
        app = create_controller_app(Controller(args.root, token, args.cameras.split(","), host))
    uvicorn.run(app, host=args.host, port=args.port, ws_max_size=32 * 1024**2, log_level="info")


if __name__ == "__main__":
    main()
