import { randomUUID } from 'node:crypto';
import { createRequire } from 'node:module';
import { chromium } from 'playwright';
const version = createRequire(import.meta.url)('playwright/package.json').version;
const clock = () => Number(process.hrtime.bigint() / 1000n);
export class Client {
    constructor(url, token) { this.url = url.replace(/\/$/, ''); this.token = token; }
    async request(path, method = 'GET', body) {
        const response = await fetch(this.url + path, { method, headers: { Authorization: 'Bearer ' + this.token, 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
        const data = await response.json();
        if (!response.ok)
            throw Object.assign(new Error(data.message || response.statusText), { code: data.error });
        return data;
    }
    async start(config = {}) { const result = await this.request('/v1/sessions', 'POST', { mode: 'text', binding: { kind: 'manual' }, recording: true, ...config }); return this.session(result.session_id); }
    session(id) { return new Session(this, id); }
}
export class Session {
    constructor(client, id) { this.client = client; this.id = id; this.path = '/v1/sessions/' + id; }
    capabilities() { return this.client.request(this.path + '/capabilities'); }
    stop() { return this.client.request(this.path + '/stop', 'POST'); }
    command(operation, options = {}) { return this.client.request(this.path + '/browser', 'POST', { operation, ...options }); }
    async connectNative() { const connection = new NativeConnection(this); try {
        await connection.start();
        return connection;
    }
    catch (error) {
        await connection.close();
        throw error;
    } }
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
