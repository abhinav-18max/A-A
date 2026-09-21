"""Python control facade for the TypeScript Playwright worker."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import grpc

from mba.generated import adapter_pb2 as pb
from mba.generated import adapter_pb2_grpc as rpc
from mba.ipc import Child
from mba.protocol import AdapterError
from mba.setup import runtime_root


class BrowserDriver:
    def __init__(self, socket: Path, environment=None, harness_url=None):
        self.child = Child(socket)
        self.environment = environment or {}
        self.harness_url = harness_url
        self.error = None
        self.task = self.health_task = None
        self.stub = None

    async def start(self, config, evidence):
        self.config, self.evidence = config, evidence
        worker = Path(
            os.environ.get("MBA_BROWSER_WORKER", Path(__file__).parent / "worker/main.cjs")
        )
        if not worker.is_file():
            raise AdapterError(
                "runtime_missing", "Build the TypeScript worker with npm run build", 503
            )
        # Development checkout dependencies; installed runtime setup can supply NODE_PATH.
        env = dict(self.environment)
        modules = Path(__file__).resolve().parents[2] / "browser-worker/node_modules"
        if modules.is_dir():
            env["NODE_PATH"] = str(modules)
        elif "NODE_PATH" not in os.environ:
            env["NODE_PATH"] = str(runtime_root() / "browser/node_modules")
        if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and (runtime_root() / "browsers").exists():
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(runtime_root() / "browsers")
        await self.child.start(["node", str(worker)], env, evidence.root / "logs/browser.log")
        self.stub = rpc.BrowserStub(self.child.channel)
        binding = config.binding.model_dump() if config.binding.kind != "manual" else {}
        harness = config.binding.kind == "harness"
        if harness:
            binding["join_selector"] = "#join"
        target = (
            str(config.target_url)
            if config.target_url
            else ("http://127.0.0.1/mba-harness" if harness else "")
        )
        origins = set(config.permission_origins)
        if target:
            p = urlsplit(target)
            origins.add(f"{p.scheme}://{p.netloc}")
        script = (Path(__file__).parent / "observer.js").read_text()
        before = evidence.clock.now_us()
        ready = await self.stub.Start(
            pb.BrowserStart(
                protocol_version=1,
                browser_control=config.browser_control,
                webmcp=config.webmcp,
                native_attach_timeout_ms=int(config.native_attach_timeout_s * 1000),
                headless=config.headless
                if config.headless is not None
                else config.mode == "text" and not (config.camera_enabled or config.visual_enabled),
                target_url=target,
                binding_json=json.dumps(binding),
                storage_state_json=json.dumps(config.storage_state) if config.storage_state else "",
                permission_origins=sorted(origins),
                observer_script=script,
                artifact_root=str(evidence.root.resolve()),
                harness_html=(Path(__file__).parent / "harness/index.html").read_text()
                if harness
                else "",
                audio=config.mode in {"audio", "multimodal"},
                camera=config.camera_enabled,
                calibrate=harness,
            ),
            timeout=config.startup_timeout_s,
        )
        # Map clocks with a short RPC after expensive browser startup.
        before = evidence.clock.now_us()
        ready = await self.stub.Health(pb.Empty(), timeout=5)
        after = evidence.clock.now_us()
        if ready.protocol_version != 1:
            raise AdapterError("protocol_mismatch", "Browser protocol mismatch")
        self.offset = (before + after) // 2 - ready.now_us
        self.uncertainty = (after - before) // 2
        self.task = asyncio.create_task(self._observe())
        self.health_task = asyncio.create_task(self._health())

    async def _observe(self):
        try:
            async for event in self.stub.Observe(pb.Empty()):
                data = json.loads(event.json)
                data["clock_uncertainty_us"] = (
                    data.get("clock_uncertainty_us", 0) + self.uncertainty
                )
                await self.evidence.emit(
                    event.type, data, max(0, event.observed_at_us + self.offset)
                )
                if event.type == "browser.download":
                    await self.evidence.artifact(
                        self.evidence.resolve(data["path"]),
                        "download",
                        max(0, event.observed_at_us + self.offset),
                        self.evidence.clock.now_us(),
                        filename=data["filename"],
                    )
                if event.type == "browser.error" and not getattr(self, "stopping", False):
                    self.error = data.get("reason", "browser_failed")
                    if callback := getattr(self, "on_failure", None):
                        asyncio.create_task(callback(self.error))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.error = str(exc)

    async def _health(self):
        try:
            while True:
                await asyncio.sleep(1)
                await self.stub.Health(pb.Empty(), timeout=3)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.error = str(exc)

    async def command(
        self,
        operation,
        selector="",
        value="",
        timeout_ms=15000,
        frame_selector="",
        page_id="",
        frame_id="",
        expected_document_id="",
        options=None,
    ):
        if self.config.browser_control == "native" and operation not in {
            "native_descriptor",
            "native_observe",
            "capabilities",
        }:
            raise AdapterError("browser_control_external", "Use the native Playwright Page")
        options = {
            **(options or {}),
            "page_id": page_id,
            "frame_id": frame_id,
            "expected_document_id": expected_document_id,
        }
        try:
            result = await self.stub.Command(
                pb.BrowserCommand(
                    operation=operation,
                    selector=selector,
                    value=value,
                    timeout_ms=timeout_ms,
                    frame_selector=frame_selector,
                    options_json=json.dumps(options),
                ),
                timeout=timeout_ms / 1000 + 2,
            )
        except grpc.aio.AioRpcError as exc:
            detail = exc.details() or "Browser operation failed"
            try:
                error = json.loads(detail.removeprefix("Error: "))
            except ValueError:
                raise AdapterError("browser_operation_failed", detail) from exc
            raise AdapterError(error["code"], error["message"]) from exc
        if operation == "bind_text":
            self.bound = bool((options.get("binding") or {}).get("input_selector"))
        return json.loads(result.json)

    async def native_descriptor(self):
        before = self.evidence.clock.now_us()
        result = await self.command("native_descriptor")
        after = self.evidence.clock.now_us()
        result["now_us"] = (before + after) // 2
        result["clock_uncertainty_us"] = (after - before) // 2 + self.uncertainty
        return result

    async def native_observe(self, rows):
        adjusted = [
            {**row, "observed_at_us": max(0, row["observed_at_us"] - self.offset)} for row in rows
        ]
        return await self.command("native_observe", options={"rows": adjusted})

    async def send_text(self, content):
        if self.config.binding.kind == "manual" and not getattr(self, "bound", False):
            raise AdapterError(
                "binding_required", "Configure a text binding before sending text", 422
            )
        return await self.command("text", value=content)

    async def checkpoint(self):
        before = self.evidence.clock.now_us()
        result = await self.command("screenshot")
        after = self.evidence.clock.now_us()
        await self.evidence.artifact(
            self.evidence.root / result["path"], "frame", before, after, surface="page_viewport"
        )
        return result

    async def calibration(self, command=None):
        result = await self.command(
            "calibration", value=command.model_dump_json() if command else ""
        )
        result["clock_offset_us"] += self.offset
        result["clock_uncertainty_us"] += self.uncertainty
        return result

    async def stop(self):
        self.stopping = True
        if self.health_task:
            self.health_task.cancel()
            await asyncio.gather(self.health_task, return_exceptions=True)
        try:
            if self.stub and self.child.process and self.child.process.returncode is None:
                await self.stub.Stop(pb.Empty(), timeout=6)
            if self.task:
                await asyncio.wait_for(asyncio.shield(self.task), 1)
        finally:
            if self.task:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            await self.child.stop()
