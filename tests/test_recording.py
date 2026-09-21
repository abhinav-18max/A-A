"""Exercise real WAV chunk writing without requiring native GStreamer."""

import wave

from mba.media import PCMRecorder, rgb_png


async def test_pcm_chunks_have_exact_frame_boundaries_and_final_tail(tmp_path):
    class Engine:
        error = None
        artifacts = []

        class Evidence:
            root = tmp_path

        evidence = Evidence()

        def publish_artifact(self, path, kind, start, end, **metadata):
            self.artifacts.append((path, start, end))

    (tmp_path / "recordings").mkdir()
    engine = Engine()
    recorder = PCMRecorder(engine, "speaker", 2)
    recorder.push(bytes(48000 * 4 * 11), 123_000)
    await recorder.stop()
    assert engine.error is None
    assert len(engine.artifacts) == 2
    for (path, start, end), frames in zip(engine.artifacts, (480000, 48000), strict=True):
        with wave.open(str(path), "rb") as audio:
            assert audio.getnframes() == frames
            assert audio.getnchannels() == 2
        assert end - start == frames * 1_000_000 // 48000
    assert engine.artifacts[1][1] == engine.artifacts[0][2]


def test_sample_encoder_writes_png_header_dimensions():
    import struct

    image = rgb_png(bytes([255, 0, 0]) * 12, 4, 3)
    assert image[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack("!II", image[16:24]) == (4, 3)
