"""Optional external Playwright controller. Returned objects are never wrapped."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import time
import uuid

from mba.protocol import AdapterError, BindingConfig


class NativeConnection:
    def __init__(self, session):
        self.session = session
        self.browser = self.context = self.page = self.playwright = None
        self.task = None
        self.error = None
        self.pages = {}
        self.frames = {}
        self.bindings = {}
        self.binding_versions = {}
        self.pending = asyncio.Queue(maxsize=1024)
        self.selected = None
        self.stopping = False

    async def refresh_clock(self):
        before = time.monotonic_ns() // 1000
        self.descriptor = await self.session.native_descriptor()
        after = time.monotonic_ns() // 1000
        self.offset = self.descriptor["now_us"] - (before + after) // 2
        self.uncertainty = (after - before) // 2 + self.descriptor.get("clock_uncertainty_us", 0)
        self.clock_updated = time.monotonic()

    async def __aenter__(self):
        try:
            from playwright.async_api import async_playwright

            await self.refresh_clock()
            installed = importlib.metadata.version("playwright")
            required = self.descriptor["playwright_version"]
            if installed.split(".")[:2] != required.split(".")[:2]:
                raise AdapterError(
                    "playwright_version_mismatch",
                    f"Install playwright=={required}; found {installed}",
                )
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.connect(
                self.descriptor["endpoint"], headers=self.descriptor["headers"]
            )
            self.context = await self.browser.new_context(**self.descriptor["context_options"])
            await self.register_context(self.context)
            self.page = await self.context.new_page()
            self.register_page(self.page)
            self.selected = self.page
            if binding := self.descriptor.get("binding"):
                if binding.get("message_selector"):
                    await self.bind_text(self.page, **binding)
            self.task = asyncio.create_task(self._observe())
            if self.descriptor.get("target_url"):
                await self.page.goto(self.descriptor["target_url"])
            binding = self.descriptor.get("binding", {})
            surface = (
                self.page.frame_locator(binding["frame_selector"])
                if binding.get("frame_selector")
                else self.page
            )
            if binding.get("join_selector"):
                await surface.locator(binding["join_selector"]).click()
            if binding.get("ready_selector"):
                await surface.locator(binding["ready_selector"]).wait_for()
            await self.session.native_observe(
                [self.row("target.ready", {"page_id": self.pages[self.page]})]
            )
            return self
        except BaseException:
            await self.close()
            raise

    async def register_context(self, context):
        if context.browser is not self.browser:
            raise AdapterError("foreign_context", "Context belongs to another browser")
        for origin in self.descriptor["permission_origins"]:
            if self.descriptor["permissions"]:
                await context.grant_permissions(self.descriptor["permissions"], origin=origin)
        context.on("page", self.register_page)
        for page in context.pages:
            self.register_page(page)

    def row(self, kind, data, at=None, uncertainty=0):
        return {
            "type": kind,
            "observed_at_us": max(0, int((at or time.monotonic_ns() // 1000) + self.offset)),
            "data": {**data, "clock_uncertainty_us": self.uncertainty + uncertainty},
        }

    def enqueue(self, kind, data):
        try:
            self.pending.put_nowait(self.row(kind, data))
        except asyncio.QueueFull:
            self.error = AdapterError("observation_overrun", "Native observation queue overflow")

    def register_page(self, page):
        if page.context.browser is not self.browser:
            raise AdapterError("foreign_page", "Page belongs to another browser")
        if page in self.pages:
            return
        pid = uuid.uuid4().hex
        self.pages[page] = pid
        self.enqueue("browser.page_created", {"page_id": pid})

        def closed():
            selected = self.selected is page
            if selected:
                self.selected = None
            self.bindings.pop(page, None)
            self.enqueue("browser.page_closed", {"page_id": pid, "selected": selected})

        def navigated(frame):
            fid = self.frames.setdefault(frame, uuid.uuid4().hex)
            self.enqueue("browser.navigation", {"page_id": pid, "frame_id": fid, "url": frame.url})

        page.on("close", closed)
        page.on("framenavigated", navigated)
        page.on(
            "pageerror",
            lambda error: self.enqueue(
                "browser.page_error", {"page_id": pid, "message": str(error)}
            ),
        )

    async def select_page(self, page):
        self.register_page(page)
        self.selected = self.page = page
        await page.bring_to_front()
        await self.session.native_observe(
            [self.row("browser.selected", {"page_id": self.pages[page]})]
        )

    async def bind_text(self, page, **binding):
        self.register_page(page)
        binding = BindingConfig(
            kind="manual", **{k: v for k, v in binding.items() if k != "kind"}
        ).model_dump()
        self.binding_versions[page] = self.binding_versions.get(page, 0) + 1
        script = self.descriptor["observer_script"].replace(
            "__MBA_CONFIG__",
            json.dumps({**binding, "_binding_version": self.binding_versions[page]}),
        )
        self.bindings[page] = (binding, script)
        # Install before caller navigation. Polling reinstalls after navigation and rebinding.
        await page.add_init_script(script)
        await self._read(page, binding, script)

    async def _read(self, page, binding, script):
        root = (
            page.frame_locator(binding["frame_selector"]) if binding.get("frame_selector") else page
        )
        target = root.locator("html")
        before = time.monotonic_ns() // 1000
        result = await target.evaluate(
            "(el,script) => { (0,eval)(script); const s=window.__mbaObservations; const result={now:performance.now(), rows:s.pending.splice(0), dropped:s.dropped, error:s.error}; s.dropped=0; return result; }",
            script,
            timeout=1000,
        )
        after = time.monotonic_ns() // 1000
        if result["error"]:
            raise AdapterError("observation_failed", result["error"])
        rows = [
            self.row(
                row["type"],
                {**row["data"], "page_id": self.pages[page], "page_url": row["pageUrl"]},
                (before + after) // 2 + (row["at"] - result["now"]) * 1000,
                (after - before) // 2,
            )
            for row in result["rows"]
        ]
        if result["dropped"]:
            rows.append(
                self.row("capture.gap", {"stream": "dom", "observations": result["dropped"]})
            )
        for i in range(0, len(rows), 256):
            await self.session.native_observe(rows[i : i + 256])

    async def _observe(self):
        try:
            while True:
                if self.error:
                    raise self.error
                if time.monotonic() - self.clock_updated > 30:
                    await self.refresh_clock()
                rows = []
                while not self.pending.empty() and len(rows) < 256:
                    rows.append(self.pending.get_nowait())
                if rows:
                    await self.session.native_observe(rows)
                for page, (binding, script) in list(self.bindings.items()):
                    if not page.is_closed():
                        try:
                            await self._read(page, binding, script)
                        except Exception as exc:
                            if isinstance(exc, AdapterError):
                                raise
                            if "SyntaxError" in str(exc):
                                raise
                            # Document and frame replacement can race an observation poll.
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
            if self.browser:
                await self.browser.close()

    async def close(self):
        self.stopping = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        try:
            if self.browser and self.browser.is_connected():
                await asyncio.gather(
                    *(context.close() for context in self.browser.contexts), return_exceptions=True
                )
            await self.session.stop()
        finally:
            if self.playwright:
                await self.playwright.stop()

    async def __aexit__(self, exc_type, *_):
        shutdown = asyncio.create_task(self.close())
        try:
            await asyncio.shield(shutdown)
        except asyncio.CancelledError:
            await shutdown
            raise
        if exc_type is None and self.error:
            raise self.error
