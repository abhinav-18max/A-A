// Remote media, events and uploads for an A&A worker. Mirrors mba.sdk in Python.
import { createReadStream, createWriteStream } from 'node:fs';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import WebSocket from 'ws';

const TERMINAL = new Set(['completed', 'cancelled', 'failed']);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

export function adapterError(code, message) {
    return Object.assign(new Error(message || code), { code });
}

export function audioFormat({ rate = 48000, channels = 1, encoding = 'S16LE' } = {}) {
    if (![16000, 24000, 48000].includes(rate) || ![1, 2].includes(channels) || encoding !== 'S16LE')
        throw new RangeError('audio requires S16LE, 16/24/48 kHz, mono/stereo');
    return { kind: 'audio', format: encoding, rate, channels };
}

export function videoFormat({ width = 1280, height = 720, fps = 30, encoding = 'RGB24' } = {}) {
    if (!(width > 0 && width <= 1920 && width % 2 === 0 && height > 0 && height <= 1080 && fps > 0 && fps <= 60)
        || !['RGB24', 'YUY2'].includes(encoding) || (encoding === 'RGB24' && width % 4))
        throw new RangeError('unsupported video geometry/format');
    return { kind: 'video', format: encoding, width, height, fps };
}

// Same framing as mba.streams.HEADER ("!QQ"): big-endian sequence, then pts in microseconds.
export function packetBytes(config) {
    if (config.kind === 'audio')
        return config.rate / 50 * config.channels * 2;
    return config.width * config.height * (config.format === 'RGB24' ? 3 : 2);
}

export function packetPts(config, sequence) {
    return config.kind === 'audio' ? sequence * 20000 : Math.floor(sequence * 1e6 / config.fps);
}

export function frame(sequence, pts, data) {
    const header = Buffer.alloc(16);
    header.writeBigUInt64BE(BigInt(sequence), 0);
    header.writeBigUInt64BE(BigInt(pts), 8);
    return Buffer.concat([header, data]);
}

// Minimal decoder for mba.v1.MediaPacket (proto/adapter.proto); tests compare it with protobuf.
function fields(buffer) {
    const out = [];
    let offset = 0;
    const varint = () => {
        let value = 0, scale = 1, byte;
        do {
            byte = buffer[offset++];
            value += (byte & 0x7f) * scale;
            scale *= 128;
        } while (byte & 0x80);
        return value;
    };
    while (offset < buffer.length) {
        const key = varint(), field = Math.floor(key / 8), wire = key & 7;
        if (wire === 0)
            out.push([field, varint()]);
        else if (wire === 2) {
            const length = varint();
            out.push([field, buffer.subarray(offset, offset + length)]);
            offset += length;
        }
        else
            throw adapterError('invalid_packet', 'Unsupported protobuf wire type ' + wire);
    }
    return out;
}

export function decodeMediaPacket(buffer) {
    const packet = { streamId: '', sequence: 0, ptsUs: 0, data: Buffer.alloc(0), format: {}, discontinuity: false, end: false };
    for (const [field, value] of fields(buffer)) {
        if (field === 1) packet.streamId = value.toString();
        else if (field === 2) packet.sequence = value;
        else if (field === 3) packet.ptsUs = value;
        else if (field === 4) packet.data = Buffer.from(value);
        else if (field === 5) {
            const names = ['', 'kind', 'encoding', 'rate', 'channels', 'width', 'height', 'fps'];
            for (const [f, v] of fields(value))
                if (names[f])
                    packet.format[names[f]] = f <= 2 ? v.toString() : v;
        }
        else if (field === 6) packet.discontinuity = !!value;
        else if (field === 7) packet.end = !!value;
    }
    return packet;
}

export function connect(client, path) {
    const url = client.url.replace(/^http/, 'ws') + path;
    const ws = new WebSocket(url, { headers: { Authorization: 'Bearer ' + client.token }, maxPayload: 8 * 1024 * 1024 });
    const queue = [], waiters = [];
    let done = null;
    const settle = () => { while (waiters.length && (queue.length || done)) waiters.shift()(); };
    ws.on('message', (data, binary) => { queue.push({ data, binary }); settle(); });
    ws.on('close', (code, reason) => { done ||= { code, reason: String(reason) }; settle(); });
    ws.on('error', error => { done ||= { error }; settle(); });
    const opened = new Promise((resolve, reject) => { ws.once('open', resolve); ws.once('error', reject); });
    return {
        ws, opened,
        async next() {
            if (!queue.length && !done)
                await new Promise(resolve => waiters.push(resolve));
            if (queue.length)
                return queue.shift();
            if (done.error)
                throw done.error;
            return null;
        },
        async json() {
            const message = await this.next();
            if (!message)
                throw adapterError('stream_closed', 'WebSocket closed');
            const data = JSON.parse(message.data.toString());
            if (data.error)
                throw adapterError(data.error, data.message);
            return data;
        },
        close() { ws.close(); },
    };
}

export class ActionHandle {
    constructor(session, id) { this.session = session; this.id = id; }
    status() { return this.session.client.request(`${this.session.path}/actions/${this.id}`); }
    async wait(timeoutMs = 130000) {
        const deadline = Date.now() + timeoutMs;
        while (true) {
            const status = await this.status();
            if (TERMINAL.has(status.state)) {
                if (status.state === 'failed')
                    throw adapterError('action_failed', status.error || 'action failed');
                return status;
            }
            if (Date.now() > deadline)
                throw adapterError('timeout', 'Action did not finish');
            await sleep(50);
        }
    }
    cancel() { return this.session.cancel(this.id); }
}

// One live input: writes exactly one 20 ms audio packet or one video frame per call, paced from zero.
export class Input {
    constructor(session, config) {
        this.session = session;
        this.config = config;
        this.index = 0;
        this.origin = undefined;
        this.closed = false;
    }
    async open() {
        try {
            const { stream_id } = await this.session.client.request(this.session.path + '/streams', 'POST', this.config);
            this.action = await this.session.submit({ tracks: [{ kind: this.config.kind, source: { kind: 'stream', stream_id } }] });
            this.socket = connect(this.session.client, `${this.session.path}/streams/${stream_id}/ws`);
            await this.socket.opened;
            const ready = await this.socket.json();
            if (!ready.ready)
                throw adapterError('stream_failed', JSON.stringify(ready));
            return this;
        }
        catch (error) {
            await this.cancel();
            throw error;
        }
    }
    async write(data) {
        if (this.closed)
            throw adapterError('stream_closed', 'Input stream is closed');
        if (data.length !== packetBytes(this.config))
            throw new RangeError(`Expected ${packetBytes(this.config)} bytes, received ${data.length}`);
        const pts = packetPts(this.config, this.index), now = performance.now();
        this.origin ??= now;
        await sleep(Math.max(0, this.origin + pts / 1000 - now));
        this.socket.ws.send(frame(this.index, pts, data));
        const ack = await this.socket.json();
        if (ack.accepted_sequence !== this.index)
            throw adapterError(ack.error || 'stream_failed', ack.message || JSON.stringify(ack));
        this.index++;
    }
    async cancel() {
        this.closed = true;
        this.socket?.close();
        if (this.action)
            await this.action.cancel().catch(() => { });
    }
    async close() {
        if (this.closed)
            return;
        try {
            this.socket.ws.send(JSON.stringify({ end: true }));
            const ack = await this.socket.json();
            if (!ack.ended)
                throw adapterError('stream_failed', JSON.stringify(ack));
            await this.action.wait();
            this.closed = true;
        }
        catch (error) {
            await this.cancel();
            throw error;
        }
    }
    async [Symbol.asyncDispose]() { await this.close(); }
}

export async function* capture(session, kind, { channels = 2, fps = 1 } = {}) {
    const socket = connect(session.client, `${session.path}/capture/${kind}/ws?channels=${channels}&fps=${fps}`);
    try {
        await socket.opened;
        while (true) {
            const message = await socket.next();
            if (!message)
                return;
            if (!message.binary) {
                const error = JSON.parse(message.data.toString());
                throw adapterError(error.error || 'capture_failed', error.message);
            }
            yield decodeMediaPacket(message.data);
        }
    }
    finally {
        socket.close();
    }
}

export async function* events(session, after = 0) {
    while (true) {
        const socket = connect(session.client, `${session.path}/events/ws?after=${after}`);
        try {
            await socket.opened;
            while (true) {
                const message = await socket.next();
                if (!message)
                    return;
                const event = JSON.parse(message.data.toString());
                if (event.error)
                    throw adapterError(event.error, event.message);
                if (event.sequence > after) {
                    after = event.sequence;
                    yield event;
                }
            }
        }
        catch (error) {
            // Reconnect after transport loss, as the Python SDK does; adapter errors propagate.
            if (!['ECONNRESET', 'ECONNREFUSED', 'EPIPE'].includes(error.code))
                throw error;
            await sleep(500);
        }
        finally {
            socket.close();
        }
    }
}

export async function upload(session, path) {
    const response = await fetch(session.client.url + session.path + '/assets', {
        method: 'POST', duplex: 'half', body: createReadStream(path),
        headers: { Authorization: 'Bearer ' + session.client.token, 'Content-Type': 'application/octet-stream' },
    });
    const data = await response.json();
    if (!response.ok)
        throw adapterError(data.error || 'http_error', data.message || response.statusText);
    return data.asset_id;
}

export async function download(session, artifactPath, destination) {
    const response = await fetch(`${session.client.url}${session.path}/artifacts/${artifactPath}`, {
        headers: { Authorization: 'Bearer ' + session.client.token },
    });
    if (!response.ok)
        throw adapterError('http_error', `Download failed: ${response.status}`);
    await pipeline(Readable.fromWeb(response.body), createWriteStream(destination));
    return destination;
}
