"""Structural interface shared by direct and remote sessions and connector bridges."""

from collections.abc import AsyncIterator
from typing import Any, Protocol

from mba.protocol import Action, Event


class SessionInterface(Protocol):
    @property
    def id(self) -> str: ...

    browser: Any
    text: Any
    webmcp: Any
    audio: Any
    camera: Any
    visual: Any
    recording: Any

    async def capabilities(self) -> dict: ...
    async def inspect(self) -> dict: ...
    async def submit(self, action: Action): ...
    async def upload(self, path) -> str: ...
    def events(self, after_sequence: int = 0) -> AsyncIterator[Event]: ...
    async def stop(self) -> dict: ...
