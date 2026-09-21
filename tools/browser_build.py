"""Build the bundled browser worker and copy its locked runtime dependency manifest."""

import shutil
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
subprocess.run(["npm", "run", "build", "--prefix", str(root / "browser-worker")], check=True)
for name in ("package.json", "package-lock.json"):
    shutil.copyfile(root / "browser-worker" / name, root / "src/mba/worker" / name)
