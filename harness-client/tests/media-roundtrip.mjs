// Drives an existing Linux worker session's media routes from TypeScript.
import { Client } from '../index.js';

const session = new Client(process.env.MBA_URL, process.env.MBA_TOKEN).session(process.env.SESSION_ID);
const report = {};
{
    // 16 kHz mono exercises resampling to the 48 kHz virtual microphone.
    await using mic = await session.audio.openInput({ rate: 16000, channels: 1 });
    for (let index = 0; index < 75; index++) {
        const pcm = Buffer.alloc(640);
        for (let sample = 0; sample < 320; sample++)
            pcm.writeInt16LE(Math.round(8000 * Math.sin(2 * Math.PI * 880 * (index * 320 + sample) / 16000)), sample * 2);
        await mic.write(pcm);
    }
}
console.log('INJECTED');
let peak = 0, announced = false;
const deadline = Date.now() + 15000;
for await (const packet of session.audio.capture({ channels: 1 })) {
    if (!announced) {
        console.log('CAPTURING');
        announced = true;
    }
    const samples = new Int16Array(packet.data.buffer, packet.data.byteOffset, packet.data.length / 2);
    for (const value of samples)
        peak = Math.max(peak, Math.abs(value));
    if (peak > 1000 || Date.now() > deadline)
        break;
}
report.speaker_peak = peak;
for await (const frame of session.visual.frames({ fps: 1 })) {
    report.frame = { bytes: frame.data.length, width: frame.format.width, height: frame.format.height, encoding: frame.format.encoding };
    break;
}
console.log(JSON.stringify(report));
