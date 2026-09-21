"""Credential-free connector demonstration; no scenario or conversation policy.

Run against a prepared Linux calibration worker to exercise bidirectional audio:
    python examples/connector_fixture.py
The connector captures target audio metadata and emits one deterministic tone.
"""

import asyncio

from mba import Adapter, AudioChunk, AudioFormat, BindingConfig, SessionConfig
from mba.connectors import Bridge, Capabilities, Output, Routes
from mba.media import sine_pcm


class FixtureConnector:
    capabilities = Capabilities(audio_input=True, audio_output=True, interruptions=True)

    async def connect(self):
        self.canceled = set()
        self.received_chunks = 0

    async def send_audio(self, chunk):
        self.received_chunks += 1

    async def outputs(self):
        data = sine_pcm(1, 440)
        for index in range(50):
            if "tone" in self.canceled:
                break
            yield Output(
                "tone",
                "audio",
                AudioChunk(
                    data[index * 1920 : (index + 1) * 1920], index, index * 20000, AudioFormat()
                ),
            )
        yield Output("tone", "end")

    async def interrupt(self, response_id):
        self.canceled.add(response_id)

    async def close(self):
        print(f"Captured {self.received_chunks} audio chunks")


async def main():
    config = SessionConfig(mode="multimodal", binding=BindingConfig(kind="harness"))
    async with Adapter(config, artifact_dir="artifacts") as session:
        async with Bridge(
            session, FixtureConnector(), Routes(target_audio=True, output_audio=True)
        ) as bridge:
            await bridge.wait()


if __name__ == "__main__":
    asyncio.run(main())
