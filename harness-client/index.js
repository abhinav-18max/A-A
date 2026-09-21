import { randomUUID } from 'node:crypto';
import { createRequire } from 'node:module';
import { chromium } from 'playwright';
import * as media from './media.js';
export { audioFormat, videoFormat, decodeMediaPacket, ActionHandle, Input } from './media.js';
const version = createRequire(import.meta.url)('playwright/package.json').version;
const clock = () => Number(process.hrtime.bigint() / 1000n);
export class Client {
    constructor(url, token) { this.url = url.replace(/\/$/, ''); this.token = token; }
    async request(path, method = 'GET', body) {
        const response = await fetch(this.url + path, { method, headers: { Authorization: 'Bearer ' + this.token, 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
        const data = await response.json();
        if (!response.ok)
            throw media.adapterError(data.error, data.message || response.statusText);
        return data;
    }
    async start(config = {}) { const result = await this.request('/v1/sessions', 'POST', { mode: 'text', binding: { kind: 'manual' }, recording: true, ...config }); return this.session(result.session_id); }
    session(id) { return new Session(this, id); }
}
export class Session {
    constructor(client, id) {
        this.client = client;
        this.id = id;
        this.path = '/v1/sessions/' + id;
        this.audio = new Media(this, 'audio');
        this.camera = new Media(this, 'video');
        this.visual = { screenshot: destination => this.screenshot(destination), frames: ({ fps = 1 } = {}) => media.capture(this, 'video', { fps }) };
        this.recording = { start: () => this.request('/recording', 'POST', { enabled: true }), stop: async () => { await this.request('/recording', 'POST', { enabled: false }); return (await this.inspect()).artifacts; } };
        this.webmcp = {
            tools: ({ pageId = '' } = {}) => this.command('webmcp_tools', { page_id: pageId }),
            call: (name, args = {}, { source = '', frameId = '', pageId = '', timeoutMs = 30000 } = {}) => this.command('webmcp_call', { value: name, frame_id: frameId, page_id: pageId, timeout_ms: timeoutMs, options: { arguments: args, source } }),
            addAdapter: adapter => this.command('webmcp_adapter', { options: { adapter } }),
            removeAdapter: name => this.command('webmcp_adapter', { options: { remove: name } }),
        };
    }
    request(path, method, body) { return this.client.request(this.path + path, method, body); }
    capabilities() { return this.request('/capabilities'); }
    inspect() { return this.request(''); }
    stop() { return this.request('/stop', 'POST'); }
    command(operation, options = {}) { return this.request('/browser', 'POST', { operation, ...options }); }
    async submit(action) { return new media.ActionHandle(this, (await this.request('/actions', 'POST', action)).action_id); }
    cancel(actionId) { return this.request(`/actions/${actionId}/cancel`, 'POST'); }
    upload(path) { return media.upload(this, path); }
    download(artifactPath, destination) { return media.download(this, artifactPath, destination); }
    async sendText(content, options = {}) { return this.submit({ tracks: [{ kind: 'text', content }], ...options }); }
    async sendAudio(path, options = {}) { return this.submit({ tracks: [{ kind: 'audio', source: { kind: 'asset', asset_id: await this.upload(path) } }], ...options }); }
    async sendVideo(path, options = {}) { return this.submit({ tracks: [{ kind: 'video', source: { kind: 'asset', asset_id: await this.upload(path) } }], ...options }); }
    stopMedia(kind) { return this.request('/media/stop', 'POST', { kind }); }
    checkpoint() { return this.request('/checkpoint', 'POST'); }
    async screenshot(destination) { const { path } = await this.checkpoint(); return destination ? this.download(path, destination) : path; }
    events(after = 0) { return media.events(this, after); }
    eventPage(after = 0) { return this.request('/events?after=' + after); }
    async connectNative() { const connection = new NativeConnection(this); try {
        await connection.start();
        return connection;
    }
    catch (error) {
        await connection.close();
        throw error;
    } }
}
export class Media {
    constructor(session, kind) { this.session = session; this.kind = kind; }
    play(path, options) { return this.kind === 'audio' ? this.session.sendAudio(path, options) : this.session.sendVideo(path, options); }
    openInput(format) {
        const config = this.kind === 'audio' ? media.audioFormat(format) : media.videoFormat(format);
        return new media.Input(this.session, config).open();
    }
    capture({ channels = 2 } = {}) {
        if (this.kind !== 'audio')
            throw media.adapterError('unsupported_operation', 'Use visual.frames()');
        return media.capture(this.session, 'audio', { channels });
    }
    stop() { return this.session.stopMedia(this.kind); }
}
export class NativeConnection {
    constructor(session) { this.session = session; this.pages = new Map(); this.bindings = new Map(); this.bindingVersions = new WeakMap(); this.pending = []; this.frameIds = new WeakMap(); this.stopping = false; this.error = null; }
    async refresh() { const before = clock(); this.descriptor = await this.session.client.request(this.session.path + '/native'); const after = clock(); this.offset = this.descriptor.now_us - (before + after) / 2; this.uncertainty = (after - before) / 2 + (this.descriptor.clock_uncertainty_us || 0); this.updated = clock(); }
    row(type, data, at = clock(), uncertainty = 0) { return { type, observed_at_us: Math.max(0, Math.round(at + this.offset)), data: { ...data, clock_uncertainty_us: this.uncertainty + uncertainty } }; }
    async send(rows) { if (rows.length)
        await this.session.client.request(this.session.path + '/native/observations', 'POST', { rows }); }
    enqueue(type, data) { if (this.pending.length >= 1024) {
        this.error = new Error('Native observation queue overflow');
        return;
    } this.pending.push(this.row(type, data)); }
    async start() {
        await this.refresh();
        if (version.split('.').slice(0, 2).join('.') !== this.descriptor.playwright_version.split('.').slice(0, 2).join('.'))
            throw new Error('Playwright version mismatch: install ' + this.descriptor.playwright_version);
        this.browser = await chromium.connect(this.session.client.url.replace(/^http/, 'ws') + this.session.path + '/native/ws', { headers: { Authorization: 'Bearer ' + this.session.client.token } });
        const options = this.descriptor.context_options;
        this.context = await this.browser.newContext({ viewport: options.viewport, storageState: options.storage_state });
        await this.registerContext(this.context);
        this.page = await this.context.newPage();
        this.registerPage(this.page);
        this.selected = this.page;
        const binding = this.descriptor.binding || {};
        if (binding.message_selector)
            await this.bindText(this.page, binding);
        this.polling = this.observe();
        if (this.descriptor.target_url)
            await this.page.goto(this.descriptor.target_url);
        const surface = binding.frame_selector ? this.page.frameLocator(binding.frame_selector) : this.page;
        if (binding.join_selector)
            await surface.locator(binding.join_selector).click();
        if (binding.ready_selector)
            await surface.locator(binding.ready_selector).waitFor();
        await this.send([this.row('target.ready', { page_id: this.pages.get(this.page) })]);
    }
    async registerContext(context) { if (context.browser() !== this.browser)
        throw new Error('Context belongs to another browser'); for (const origin of this.descriptor.permission_origins)
        if (this.descriptor.permissions.length)
            await context.grantPermissions(this.descriptor.permissions, { origin }); context.on('page', page => this.registerPage(page)); for (const page of context.pages())
        this.registerPage(page); }
    registerPage(page) {
        if (page.context().browser() !== this.browser)
            throw new Error('Page belongs to another browser');
        if (this.pages.has(page))
            return;
        const id = randomUUID();
        this.pages.set(page, id);
        this.enqueue('browser.page_created', { page_id: id });
        page.on('close', () => { const selected = this.selected === page; if (selected)
            this.selected = undefined; this.bindings.delete(page); this.enqueue('browser.page_closed', { page_id: id, selected }); });
        page.on('framenavigated', frame => { if (!this.frameIds.has(frame))
            this.frameIds.set(frame, randomUUID()); this.enqueue('browser.navigation', { page_id: id, frame_id: this.frameIds.get(frame), url: frame.url() }); });
        page.on('pageerror', error => this.enqueue('browser.page_error', { page_id: id, message: String(error) }));
    }
    async selectPage(page) { this.registerPage(page); this.selected = this.page = page; await page.bringToFront(); await this.send([this.row('browser.selected', { page_id: this.pages.get(page) })]); }
    async bindText(page, binding) {
        this.registerPage(page);
        const value = { message_id_attribute: 'data-message-id', ...binding };
        this.bindingVersions.set(page, (this.bindingVersions.get(page) || 0) + 1);
        const script = this.descriptor.observer_script.replace('__MBA_CONFIG__', () => JSON.stringify({ ...value, _binding_version: this.bindingVersions.get(page) }));
        this.bindings.set(page, { binding: value, script });
        await page.addInitScript({ content: script });
        await this.read(page, value, script);
    }
    frameId(frame) { if (!this.frameIds.has(frame))
        this.frameIds.set(frame, randomUUID()); return this.frameIds.get(frame); }
    async webmcpTools(page = this.selected) {
        if (!this.descriptor.webmcp)
            throw media.adapterError('webmcp_disabled', 'Start the session with webmcp enabled');
        this.registerPage(page);
        const tools = [], blocked = [];
        for (const frame of page.frames()) {
            const where = { page_id: this.pages.get(page), frame_id: this.frameId(frame), frame_url: frame.url() };
            let listing;
            try {
                listing = await Promise.race([frame.evaluate(`(${this.descriptor.webmcp_list_script})()`), new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 5000))]);
            }
            catch (error) {
                if (/permissions policy|NotAllowedError/.test(String(error)))
                    blocked.push({ ...where, reason: 'permissions_policy' });
                continue;
            }
            for (const tool of listing.tools)
                tools.push({ ...tool, ...where, tool_source: 'site' });
        }
        return { page_id: this.pages.get(page), tools, blocked_frames: blocked };
    }
    async webmcpCall(page, name, args = {}, { frame, timeoutMs = 30000 } = {}) {
        if (!frame) {
            const frames = new Set((await this.webmcpTools(page)).tools.filter(t => t.name === name).map(t => t.frame_id));
            if (frames.size > 1)
                throw media.adapterError('webmcp_ambiguous_tool', `Tool ${name} is in several frames`);
            frame = page.frames().find(f => frames.has(this.frameId(f))) || page.mainFrame();
        }
        const where = { page_id: this.pages.get(page), frame_id: this.frameId(frame), tool: name };
        this.enqueue('webmcp.invoked', { ...where, initiator: 'adapter', tool_source: 'site' });
        let timer;
        const result = await Promise.race([
            frame.evaluate(`(${this.descriptor.webmcp_call_script})(${JSON.stringify({ name, inputJson: JSON.stringify(args) })})`),
            new Promise((_, reject) => { timer = setTimeout(() => reject(media.adapterError('webmcp_timeout', `Tool ${name} did not respond`)), timeoutMs); }),
        ]).finally(() => clearTimeout(timer));
        if (result.status === 'unavailable' || result.status === 'not_found')
            throw media.adapterError('webmcp_tool_not_found', `No WebMCP tool named ${name}`);
        this.enqueue('webmcp.responded', { ...where, status: result.status });
        let parsed = result.output ?? null;
        try {
            parsed = result.output === undefined ? null : JSON.parse(result.output);
        }
        catch { }
        return { ...where, status: result.status, result: parsed, error: result.error, untrusted: true, tool_source: 'site' };
    }
    async read(page, binding, script) {
        const root = binding.frame_selector ? page.frameLocator(binding.frame_selector) : page;
        const before = clock();
        const result = await root.locator('html').evaluate((el, script) => { (0, eval)(script); const s = window.__mbaObservations; const result = { now: performance.now(), rows: s.pending.splice(0), dropped: s.dropped, error: s.error }; s.dropped = 0; return result; }, script, { timeout: 1000 });
        const after = clock();
        if (result.error)
            throw new Error(result.error);
        const rows = result.rows.map(row => this.row(row.type, { ...row.data, page_id: this.pages.get(page), page_url: row.pageUrl }, (before + after) / 2 + (row.at - result.now) * 1000, (after - before) / 2));
        if (result.dropped)
            rows.push(this.row('capture.gap', { stream: 'dom', observations: result.dropped }));
        for (let i = 0; i < rows.length; i += 256)
            await this.send(rows.slice(i, i + 256));
    }
    async observe() {
        try {
            while (!this.stopping) {
                if (this.error)
                    throw this.error;
                if (clock() - this.updated > 30e6)
                    await this.refresh();
                await this.send(this.pending.splice(0, 256));
                for (const [page, { binding, script }] of this.bindings)
                    if (!page.isClosed()) {
                        try {
                            await this.read(page, binding, script);
                        }
                        catch (error) {
                            if (!String(error).includes('Execution context was destroyed') && !String(error).includes('Timeout'))
                                throw error;
                        }
                    }
                await new Promise(resolve => setTimeout(resolve, 50));
            }
        }
        catch (error) {
            if (!this.stopping) {
                this.error = error;
                await this.browser.close().catch(() => { });
            }
        }
    }
    async close() { this.stopping = true; await this.polling; if (this.browser?.isConnected())
        await Promise.allSettled(this.browser.contexts().map(context => context.close())); await this.session.stop(); }
    async [Symbol.asyncDispose]() { await this.close(); if (this.error)
        throw this.error; }
}
