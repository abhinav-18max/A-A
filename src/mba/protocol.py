from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


def new_id() -> str:
    return uuid4().hex


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BindingConfig(Model):
    kind: Literal["harness", "declarative", "manual"] = "harness"
    frame_selector: str | None = None
    ready_selector: str = "#ready"
    join_selector: str | None = None
    input_selector: str = "#composer"
    submit_selector: str | None = "#send"
    acknowledgement_selector: str | None = None
    message_selector: str = "[data-role='assistant']"
    message_id_attribute: str = "data-message-id"
    leave_selector: str | None = None
    disconnected_selector: str | None = None
    supports_text: bool = True
    supports_audio: bool = True
    supports_video: bool = True


class RemoteBrowser(Model):
    """A remote Chrome reached over CDP, such as a Browserbase session. Text and browser only:
    its microphone, speaker, camera and display are not A&A's, so media and recording are off."""

    cdp_url: str = Field(pattern=r"^(wss?|https?)://", description="CDP WebSocket or HTTP endpoint")
    headers: dict[str, str] = Field(default_factory=dict)


class SessionConfig(Model):
    browser_control: Literal["tools", "native"] = "tools"
    native_attach_timeout_s: float = Field(default=120, gt=0, le=3600)
    webmcp: bool = Field(
        default=False,
        description="Enable Chromium's WebMCP API so pages can register tools for the session.",
    )
    mode: Literal["text", "audio", "video", "multimodal"] = "multimodal"
    camera: bool | None = None
    visual: bool | None = None
    headless: bool | None = None
    recording: bool = True
    persistent_events: bool = True
    target_url: HttpUrl | None = None
    binding: BindingConfig = Field(default_factory=BindingConfig)
    permission_origins: list[str] = Field(default_factory=list)
    storage_state: dict | None = Field(default=None, exclude=True)
    startup_timeout_s: float = Field(default=60, gt=0, le=300)
    action_timeout_s: float = Field(default=120, gt=0, le=3600)
    max_duration_s: float = Field(default=1800, gt=0, le=86400)
    capture_tail_s: float = Field(default=2, ge=0, le=10)
    min_free_disk_mb: int = Field(default=1024, ge=1)
    # Library-only: contains credentials and a network destination, so it is never serialized.
    remote_browser: RemoteBrowser | None = Field(default=None, exclude=True)

    def request_body(self):
        """JSON for the HTTP API. WebMCP is sent only when enabled, so plain sessions still start
        on workers that predate it."""
        body = self.model_dump(mode="json")
        if not self.webmcp:
            body.pop("webmcp")
        if self.storage_state is not None:
            body["storage_state"] = self.storage_state
        return body

    @property
    def camera_enabled(self):
        return self.camera if self.camera is not None else self.mode in {"video", "multimodal"}

    @property
    def visual_enabled(self):
        return (
            self.visual
            if self.visual is not None
            else self.recording or self.mode in {"video", "multimodal"}
        )

    @model_validator(mode="after")
    def target_for_binding(self):
        if self.browser_control == "native" and self.binding.kind == "harness":
            raise ValueError("native control requires a manual or declarative binding")
        if self.binding.kind == "harness" and (
            self.mode not in {"audio", "multimodal"} or not self.camera_enabled
        ):
            raise ValueError(
                "the calibration harness requires microphone and camera; use a manual/declarative binding for other modes"
            )
        if self.binding.kind == "declarative" and self.target_url is None:
            raise ValueError("declarative bindings require target_url")
        if self.binding.kind == "harness" and self.target_url is not None:
            raise ValueError("harness binding uses the worker's local calibration website")
        if self.remote_browser and (
            self.browser_control != "tools"
            or self.mode != "text"
            or self.recording
            or self.camera_enabled
            or self.visual_enabled
        ):
            raise ValueError(
                "remote_browser supports tools-mode text sessions only; set recording=False"
            )
        return self

    def reject_remote_browser(self):
        """HTTP and MCP callers must not choose where the worker connects."""
        if self.remote_browser is not None:
            raise AdapterError(
                "remote_browser_library_only",
                "remote_browser is available only to the in-process library",
                422,
            )


class AssetSource(Model):
    kind: Literal["asset"] = "asset"
    asset_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class StreamSource(Model):
    kind: Literal["stream"] = "stream"
    stream_id: str = Field(pattern=r"^[a-f0-9]{32}$")


Source = Annotated[AssetSource | StreamSource, Field(discriminator="kind")]


class TextTrack(Model):
    kind: Literal["text"] = "text"
    content: str = Field(min_length=1, max_length=100_000)
    offset_us: Literal[0] = 0


class AudioTrack(Model):
    kind: Literal["audio"] = "audio"
    source: Source
    offset_us: int = Field(default=0, ge=0, le=60_000_000)


class VideoTrack(Model):
    kind: Literal["video"] = "video"
    source: Source
    offset_us: int = Field(default=0, ge=0, le=60_000_000)


Track = Annotated[TextTrack | AudioTrack | VideoTrack, Field(discriminator="kind")]


class Action(Model):
    action_id: str = Field(default_factory=new_id, pattern=r"^[A-Za-z0-9_-]{1,100}$")
    tracks: list[Track] = Field(min_length=1, max_length=3)
    start_at_us: int | None = Field(default=None, ge=0)
    replace: bool = False

    @model_validator(mode="after")
    def unique_tracks(self):
        kinds = [t.kind for t in self.tracks]
        if len(kinds) != len(set(kinds)):
            raise ValueError("only one track per modality is allowed")
        return self


class ActionState(StrEnum):
    ACCEPTED = "accepted"
    STARTED = "started"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_ACTIONS = {ActionState.COMPLETED, ActionState.CANCELLED, ActionState.FAILED}


class ActionStatus(Model):
    action_id: str
    state: ActionState
    accepted_at_us: int
    started_at_us: int | None = None
    ended_at_us: int | None = None
    error: str | None = None


class Event(Model):
    schema_version: Literal[1] = 1
    id: str = Field(default_factory=new_id)
    session_id: str
    sequence: int
    observed_at_us: int
    emitted_at_us: int
    type: str
    data: dict = Field(default_factory=dict)


class StreamConfig(Model):
    kind: Literal["audio", "video"]
    format: Literal["S16LE", "YUY2", "RGB24"]
    rate: int = 48000
    channels: int = 1
    width: int = 1280
    height: int = 720
    fps: int = 30

    @model_validator(mode="after")
    def supported_format(self):
        if self.kind == "audio" and (
            self.format != "S16LE"
            or self.rate not in (16000, 24000, 48000)
            or self.channels not in (1, 2)
        ):
            raise ValueError("live audio requires S16LE, 16/24/48 kHz, mono/stereo")
        if self.kind == "video" and (
            self.format not in ("YUY2", "RGB24")
            or not 0 < self.width <= 1920
            or self.width % (4 if self.format == "RGB24" else 2)
            or not 0 < self.height <= 1080
            or not 0 < self.fps <= 60
        ):
            raise ValueError("unsupported video geometry/format")
        return self

    @property
    def packet_bytes(self):
        if self.kind == "audio":
            return self.rate // 50 * self.channels * 2
        return self.width * self.height * (3 if self.format == "RGB24" else 2)

    def pts(self, sequence):
        return sequence * 20_000 if self.kind == "audio" else sequence * 1_000_000 // self.fps


class CalibrationCommand(Model):
    kind: Literal["tone", "visual"]
    frequency: float = Field(default=997, ge=100, le=10000)
    duration_ms: int = Field(default=300, ge=20, le=10000)
    delay_ms: int = Field(default=0, ge=0, le=10000)
    marker_id: int = 0
    color: str = Field(default="#38bdf8", pattern=r"^#[a-fA-F0-9]{6}$")


class AdapterError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class BrowserOperation(Model):
    operation: Literal[
        "open",
        "click",
        "fill",
        "press",
        "wait",
        "screenshot",
        "text",
        "snapshot",
        "pages",
        "new_page",
        "select_page",
        "close_page",
        "back",
        "forward",
        "reload",
        "double_click",
        "hover",
        "drag",
        "click_at",
        "type",
        "select",
        "check",
        "uncheck",
        "scroll",
        "scroll_into_view",
        "upload",
        "downloads",
        "dialog",
        "bind_text",
        "webmcp_tools",
        "webmcp_call",
        "webmcp_adapter",
    ]
    selector: str = ""
    value: str = ""
    timeout_ms: int = Field(default=15000, gt=0, le=120000)
    frame_selector: str = ""
    page_id: str = ""
    frame_id: str = ""
    expected_document_id: str = ""
    options: dict = Field(
        default_factory=dict,
        description=(
            "Operation-specific options: target={kind:css,value}, {kind:role,role,name,exact}, "
            "{kind:text,text,exact}, or {kind:test_id,value}; drag uses destination (CSS) or "
            "destination_target; click_at/scroll use x,y; select uses values; wait uses state "
            "(visible,hidden,attached,detached); dialog uses accept,prompt; bind_text uses binding. "
            "For upload, value is an asset ID returned by upload, not a worker filesystem path. "
            "webmcp_call takes the tool name as value and uses arguments (object), optional "
            "source (site or tester) and frame_id; webmcp_adapter uses adapter (a SiteAdapter) "
            "or remove (an adapter name)."
        ),
    )


STEP_OPERATION = Literal[
    "open",
    "click",
    "fill",
    "press",
    "wait",
    "screenshot",
    "snapshot",
    "pages",
    "back",
    "forward",
    "reload",
    "double_click",
    "hover",
    "drag",
    "click_at",
    "type",
    "select",
    "check",
    "uncheck",
    "scroll",
    "scroll_into_view",
    "upload",
    "downloads",
    "dialog",
]
TOOL_NAME = r"^[A-Za-z0-9_.-]{1,64}$"


class AdapterStep(Model):
    """One browser operation. String fields may use {{argument}} placeholders."""

    operation: STEP_OPERATION
    selector: str = ""
    value: str = ""
    timeout_ms: int | None = Field(default=None, gt=0, le=120000)
    frame_selector: str = ""
    options: dict = Field(default_factory=dict)


class AdapterTool(Model):
    name: str = Field(pattern=TOOL_NAME)
    description: str = Field(default="", max_length=2000)
    input_schema: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})
    steps: list[AdapterStep] = Field(min_length=1, max_length=50)
    read_only: bool = False


class SiteAdapter(Model):
    """Tester-defined tools for a site without WebMCP, run with real Playwright input."""

    name: str = Field(pattern=TOOL_NAME)
    description: str = ""
    url_pattern: str = Field(default="", description="Glob such as https://example.com/*")
    tools: list[AdapterTool] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_tools(self):
        names = [t.name for t in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique within an adapter")
        return self


class RecordingOperation(Model):
    enabled: bool
