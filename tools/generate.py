"""Generate internal Python bindings; TypeScript uses npm run generate."""

import os
import subprocess
import sys
from pathlib import Path

from grpc_tools import protoc

root = Path(__file__).resolve().parents[1]
out = root / "src/mba/generated"
result = protoc.main(
    [
        "protoc",
        f"-I{root / 'proto'}",
        f"--python_out={out}",
        f"--grpc_python_out={out}",
        str(root / "proto/adapter.proto"),
    ]
)
if result:
    raise SystemExit(result)
p = out / "adapter_pb2_grpc.py"
p.write_text(p.read_text().replace("import adapter_pb2 as", "from . import adapter_pb2 as"))

if "--typescript" in sys.argv:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{root / 'proto'}",
            f"--plugin=protoc-gen-ts_proto={root / 'browser-worker/node_modules/.bin/protoc-gen-ts_proto'}",
            f"--ts_proto_out={root / 'browser-worker/src/generated'}",
            "--ts_proto_opt=outputServices=grpc-js,env=node,esModuleInterop=true,forceLong=number",
            str(root / "proto/adapter.proto"),
        ],
        check=True,
    )

if "--rust" in sys.argv:
    subprocess.run(
        [
            "cargo",
            "check",
            "--locked",
            "--manifest-path",
            str(root / "media-core/Cargo.toml"),
            "--no-default-features",
        ],
        env={**os.environ, "MBA_UPDATE_GENERATED": "1"},
        check=True,
    )
