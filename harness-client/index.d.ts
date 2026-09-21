import type { Browser, BrowserContext, Page } from 'playwright';
export class Client {
    constructor(url: string, token: string);
    start(config?: Record<string, unknown>): Promise<Session>;
    session(id: string): Session;
    request(path: string, method?: string, body?: unknown): Promise<any>;
}
export class Session {
    id: string;
    capabilities(): Promise<Record<string, unknown>>;
    stop(): Promise<Record<string, unknown>>;
    command(operation: string, options?: Record<string, unknown>): Promise<any>;
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
    close(): Promise<void>;
    [Symbol.asyncDispose](): Promise<void>;
}
