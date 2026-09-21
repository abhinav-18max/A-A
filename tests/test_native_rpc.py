"""Cross-language transport check; no Linux devices are needed for protocol rejection."""

import os
import tempfile
from pathlib import Path

import grpc
import pytest

from mba.generated import adapter_pb2 as pb
from mba.generated import adapter_pb2_grpc as rpc
from mba.ipc import Child


@pytest.mark.skipif(not os.environ.get("MBA_NATIVE_TEST_BINARY"), reason="requires native binary")
async def test_python_rust_unix_socket_protocol(tmp_path):
    with tempfile.TemporaryDirectory(prefix="mba-rpc-", dir="/tmp") as sockets:
        child = Child(Path(sockets) / "media.sock")
        try:
            await child.start([os.environ["MBA_NATIVE_TEST_BINARY"]], {}, tmp_path / "media.log")
            stub = rpc.MediaStub(child.channel)
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await stub.Start(pb.MediaStart(protocol_version=99), timeout=5)
            assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
            assert "protocol version" in error.value.details()
        finally:
            await child.stop()
