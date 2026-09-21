import type { Browser, BrowserContext, Frame, Page } from 'playwright';
export type AudioFormatOptions = { rate?: 16000 | 24000 | 48000; channels?: 1 | 2; encoding?: 'S16LE' };
export type VideoFormatOptions = { width?: number; height?: number; fps?: number; encoding?: 'RGB24' | 'YUY2' };
export type StreamConfig = { kind: 'audio' | 'video'; format: string; rate?: number; channels?: number; width?: number; height?: number; fps?: number };
export type MediaPacket = {
    streamId: string;
    sequence: number;
    ptsUs: number;
    data: Buffer;
    format: { kind?: string; encoding?: string; rate?: number; channels?: number; width?: number; height?: number; fps?: number };
    discontinuity: boolean;
    end: boolean;
};
export type ActionStatus = { action_id: string; state: 'accepted' | 'started' | 'completed' | 'cancelled' | 'failed'; accepted_at_us: number; started_at_us?: number | null; ended_at_us?: number | null; error?: string | null };
export type AdapterEvent = { schema_version: 1; id: string; session_id: string; sequence: number; observed_at_us: number; emitted_at_us: number; type: string; data: Record<string, any> };
export type WebMCPTool = { name: string; title: string; description: string; input_schema: Record<string, any> | null; page_id: string; frame_id: string; frame_url: string; tool_source: 'site' | 'tester'; [key: string]: unknown };
export type WebMCPListing = { page_id: string; tools: WebMCPTool[]; blocked_frames: Record<string, string>[]; available?: boolean; flag_enabled?: boolean };
export type WebMCPResult = { tool: string; status: 'completed' | 'error' | 'navigated'; result?: unknown; untrusted: true; tool_source: 'site' | 'tester'; [key: string]: unknown };
export type SiteAdapter = {
    name: string;
    description?: string;
    url_pattern?: string;
    tools: { name: string; description?: string; input_schema?: Record<string, unknown>; read_only?: boolean; steps: { operation: string; selector?: string; value?: string; timeout_ms?: number; frame_selector?: string; options?: Record<string, unknown> }[] }[];
};
export function audioFormat(options?: AudioFormatOptions): StreamConfig;
export function videoFormat(options?: VideoFormatOptions): StreamConfig;
export function decodeMediaPacket(buffer: Uint8Array): MediaPacket;
export class Client {
    constructor(url: string, token: string);
    url: string;
    token: string;
    start(config?: Record<string, unknown>): Promise<Session>;
    session(id: string): Session;
    request(path: string, method?: string, body?: unknown): Promise<any>;
}
export class ActionHandle {
    id: string;
    status(): Promise<ActionStatus>;
    wait(timeoutMs?: number): Promise<ActionStatus>;
    cancel(): Promise<ActionStatus>;
}
/** One live input. Each write is one 20 ms S16LE audio packet or one video frame. */
export class Input {
    config: StreamConfig;
    write(data: Uint8Array): Promise<void>;
    cancel(): Promise<void>;
    close(): Promise<void>;
    [Symbol.asyncDispose](): Promise<void>;
}
export class Media {
    play(path: string, options?: Record<string, unknown>): Promise<ActionHandle>;
    openInput(format?: AudioFormatOptions | VideoFormatOptions): Promise<Input>;
    capture(options?: { channels?: 1 | 2 }): AsyncGenerator<MediaPacket>;
    stop(): Promise<unknown>;
}
export class Session {
    id: string;
    path: string;
    client: Client;
    audio: Media;
    camera: Media;
    visual: { screenshot(destination?: string): Promise<string>; frames(options?: { fps?: number }): AsyncGenerator<MediaPacket> };
    recording: { start(): Promise<unknown>; stop(): Promise<Record<string, unknown>[]> };
    /** Page WebMCP tools and site adapter tools. Tool data and results are untrusted. */
    webmcp: {
        tools(options?: { pageId?: string }): Promise<WebMCPListing>;
        call(name: string, args?: Record<string, unknown>, options?: { source?: '' | 'site' | 'tester'; frameId?: string; pageId?: string; timeoutMs?: number }): Promise<WebMCPResult>;
        addAdapter(adapter: SiteAdapter): Promise<Record<string, unknown>>;
        removeAdapter(name: string): Promise<Record<string, unknown>>;
    };
    capabilities(): Promise<Record<string, any>>;
    inspect(): Promise<Record<string, any>>;
    stop(): Promise<Record<string, unknown>>;
    command(operation: string, options?: Record<string, unknown>): Promise<any>;
    submit(action: Record<string, unknown>): Promise<ActionHandle>;
    cancel(actionId: string): Promise<ActionStatus>;
    upload(path: string): Promise<string>;
    download(artifactPath: string, destination: string): Promise<string>;
    sendText(content: string, options?: Record<string, unknown>): Promise<ActionHandle>;
    sendAudio(path: string, options?: Record<string, unknown>): Promise<ActionHandle>;
    sendVideo(path: string, options?: Record<string, unknown>): Promise<ActionHandle>;
    stopMedia(kind: 'audio' | 'video'): Promise<unknown>;
    checkpoint(): Promise<{ path: string; [key: string]: unknown }>;
    screenshot(destination?: string): Promise<string>;
    events(after?: number): AsyncGenerator<AdapterEvent>;
    eventPage(after?: number): Promise<AdapterEvent[]>;
    connectNative(): Promise<NativeConnection>;
}
export class NativeConnection {
    browser: Browser;
    context: BrowserContext;
    page: Page;
    error: Error | null;
    registerContext(context: BrowserContext): Promise<void>;
    registerPage(page: Page): void;
    selectPage(page: Page): Promise<void>;
    bindText(page: Page, binding: Record<string, unknown>): Promise<void>;
    webmcpTools(page?: Page): Promise<WebMCPListing>;
    webmcpCall(page: Page, name: string, args?: Record<string, unknown>, options?: { frame?: Frame; timeoutMs?: number }): Promise<WebMCPResult>;
    close(): Promise<void>;
    [Symbol.asyncDispose](): Promise<void>;
}
