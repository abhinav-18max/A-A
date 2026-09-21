"""Check native plugins without loading Python GI or touching host devices."""

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--plugins-only", action="store_true")
parser.add_argument("--camera", default="/dev/video10")
args = parser.parse_args()
elements = [
    "appsrc",
    "appsink",
    "uridecodebin",
    "audioconvert",
    "audioresample",
    "audiomixer",
    "volume",
    "opusenc",
    "pulsesrc",
    "pulsesink",
    "v4l2sink",
    "ximagesrc",
    "videoconvert",
    "videoscale",
    "videorate",
    "vp8enc",
    "webmmux",
    "splitmuxsink",
    "queue",
    "tee",
    "filesink",
]
checks = {
    element: subprocess.run(["gst-inspect-1.0", element], capture_output=True).returncode == 0
    for element in elements
}
checks["media_binary"] = bool(shutil.which("mba-media"))
checks["node"] = bool(shutil.which("node"))
if not args.plugins_only:
    checks["camera"] = Path(args.camera).exists() and os.access(args.camera, os.R_OK | os.W_OK)
    checks["non_root"] = os.getuid() != 0
print(json.dumps({"ok": all(checks.values()), "checks": checks}, indent=2))
raise SystemExit(0 if all(checks.values()) else 1)
