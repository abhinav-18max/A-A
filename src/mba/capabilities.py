"""Capabilities describe the running session, not the physical host in general."""


async def session_capabilities(core):
    browser = await core.runtime.browser.command("capabilities")
    config = core.config
    audio = config.mode in {"audio", "multimodal"}
    return {
        **browser,
        "session_id": core.id,
        "audio_input": audio,
        "audio_output": audio,
        "camera_input": config.camera_enabled,
        "visual_output": config.visual_enabled,
        "text_output": config.browser_control == "tools",
        "audio_formats": [
            {"encoding": "S16LE", "rate": r, "channels": c}
            for r in (16000, 24000, 48000)
            for c in (1, 2)
        ]
        if audio
        else [],
        "audio_chunk_ms": 20,
        "video_formats": ["RGB24", "YUY2"] if config.camera_enabled else [],
        "video_limits": {"width": 1920, "height": 1080, "fps": 60},
        "capture_scope": {"audio": "session", "visual": "display"},
    }
