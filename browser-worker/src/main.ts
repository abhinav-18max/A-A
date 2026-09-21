import * as grpc from '@grpc/grpc-js';
import { chromium, type Browser, type BrowserServer as PWServer, type BrowserContext, type Page, type Frame, type Locator } from 'playwright';
import { mkdir, chmod } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { createRequire } from 'node:module';
import { nativeGateway } from './native-gateway.js';
import * as webmcp from './webmcp.js';
import { BrowserService, type BrowserServer, type Observation, type BrowserStart, type BrowserCommand } from './generated/adapter.js';
const version: string = createRequire(process.argv[1])('playwright/package.json').version;
const epoch = process.hrtime.bigint();
const now = () => Number((process.hrtime.bigint() - epoch) / 1000n);
let browser: Browser | undefined, browserServer: PWServer | undefined, context: BrowserContext | undefined, page: Page | undefined;
let gateway: Awaited<ReturnType<typeof nativeGateway>> | undefined;
let config: BrowserStart, binding: Record<string, any> = {};
let stopping = false, fault = '', polling: NodeJS.Timeout | undefined, attachTimer: NodeJS.Timeout | undefined, busy = false;
let nativeConnected = false, nativeReady = false;
let sequence = 0, mutationTail: Promise<unknown> = Promise.resolve();
const pages = new Map<string, Page>(), pageIds = new WeakMap<Page, string>(), frameIds = new WeakMap<Frame, string>(), documents = new WeakMap<Frame, string>();
const downloads: Record<string, string>[] = [];
const adapters = new Map<string, webmcp.SiteAdapter>(), webmcpCalls: webmcp.Pending = [];
const dialogPolicies = new WeakMap<Page, {
    accept: boolean;
    prompt?: string;
}>();
// A false write() is ordinary backpressure (object streams buffer ~16 messages), so each
// subscriber keeps an ordered backlog that is resumed on 'drain'. Only a backlog that keeps
// growing means the consumer is too slow.
type Listener = { backlog: Observation[], blocked: boolean };
const listeners = new Map<grpc.ServerWritableStream<any, Observation>, Listener>();
const pending: Observation[] = [];
function deliver(call: grpc.ServerWritableStream<any, Observation>, listener: Listener) {
    listener.blocked = false;
    while (listener.backlog.length)
        if (!call.write(listener.backlog.shift()!)) {
            listener.blocked = true;
            call.once('drain', () => deliver(call, listener));
            return;
        }
}
function fail(code: string, message = code): never { throw new Error(JSON.stringify({ code, message })); }
function emit(type: string, data: unknown, at = now()) {
    const event = { type, json: JSON.stringify(data), observedAtUs: Math.max(0, Math.round(at)), sequence: ++sequence };
    if (!listeners.size) {
        if (pending.length === 1000) {
            fault = 'browser observation backlog exceeded';
            return;
        }
        pending.push(event);
    }
    for (const [call, listener] of listeners) {
        if (listener.backlog.length >= 1000) {
            // Losing journal events silently is worse than failing the session.
            fault = 'observation_overrun';
            call.destroy(new Error('observation consumer too slow'));
            listeners.delete(call);
            continue;
        }
        listener.backlog.push(event);
        if (!listener.blocked)
            deliver(call, listener);
    }
}
function frameId(frame: Frame) { if (!frameIds.has(frame))
    frameIds.set(frame, randomUUID()); return frameIds.get(frame)!; }
function documentId(frame: Frame) { if (!documents.has(frame))
    documents.set(frame, randomUUID()); return documents.get(frame)!; }
function track(p: Page) {
    if (pageIds.has(p))
        return;
    const id = randomUUID();
    pages.set(id, p);
    pageIds.set(p, id);
    p.on('framenavigated', f => { documents.set(f, randomUUID()); emit('browser.navigation', { page_id: id, frame_id: frameId(f), document_id: documentId(f), url: f.url() }); });
    p.on('close', () => { pages.delete(id); if (page === p) {
        page = undefined;
        binding = {};
    } emit('browser.page_closed', { page_id: id }); });
    p.on('crash', () => { fault = 'page_crashed'; emit('browser.error', { reason: fault }); });
    p.on('pageerror', error => emit('browser.page_error', { page_id: id, message: error.message }));
    p.on('console', message => { if (message.type() === 'error')
        emit('browser.console', { page_id: id, level: 'error', message: message.text() }); });
    p.on('dialog', async (dialog) => { const policy = dialogPolicies.get(p); emit('browser.dialog', { page_id: id, type: dialog.type(), message: dialog.message() }); try {
        if (policy?.accept)
            await dialog.accept(policy.prompt);
        else
            await dialog.dismiss();
    }
    catch { } });
    p.on('download', async (download) => {
        try {
            const path = 'downloads/' + randomUUID();
            await mkdir(join(config.artifactRoot, 'downloads'), { recursive: true });
            await download.saveAs(join(config.artifactRoot, path));
            downloads.push({ path, filename: download.suggestedFilename(), page_id: id });
            emit('browser.download', downloads.at(-1));
        }
        catch (error) {
            emit('browser.download_failed', { page_id: id, message: String(error) });
        }
    });
    emit('browser.page_created', { page_id: id });
    // Origin-trial sites expose WebMCP without the flag, so registrations are always journaled.
    void webmcp.observe(p, id, emit, webmcpCalls);
}
function surface(p: Page, frame?: string) { return frame ? p.frameLocator(frame) : p; }
async function observe() {
    if (busy || stopping || !page || !binding.message_selector || config.browserControl === 'native')
        return;
    busy = true;
    try {
        const before = now();
        const target = surface(page, binding.frame_selector).locator('html');
        if (config.observerScript)
            await target.evaluate((_, script) => (0, eval)(script), config.observerScript.replace('__MBA_CONFIG__', () => JSON.stringify(binding)), { timeout: 1000 });
        const batch = await target.evaluate(() => {
            const s = (window as any).__mbaObservations;
            const result = { now: performance.now(), rows: s?.pending.splice(0) || [], dropped: s?.dropped || 0, error: s?.error };
            if (s)
                s.dropped = 0;
            return result;
        }, undefined, { timeout: 1000 });
        const after = now(), offset = (before + after) / 2 - batch.now * 1000;
        if (batch.error)
            throw new Error(batch.error);
        for (const row of batch.rows)
            emit(row.type, { ...row.data, page_id: pageIds.get(page), page_url: row.pageUrl, clock_uncertainty_us: (after - before) / 2 }, row.at * 1000 + offset);
        if (batch.dropped)
            emit('capture.gap', { stream: 'dom', observations: batch.dropped });
        if (binding.disconnected_selector && await surface(page, binding.frame_selector).locator(binding.disconnected_selector).isVisible())
            fault = 'target_disconnected';
    }
    catch (e) {
        if (String(e).includes('SyntaxError'))
            fault = String(e);
    }
    finally {
        busy = false;
    }
}
const operations = ['open', 'click', 'fill', 'press', 'wait', 'screenshot', 'text', 'snapshot', 'pages', 'new_page', 'select_page', 'close_page', 'back', 'forward', 'reload', 'double_click', 'hover', 'drag', 'click_at', 'type', 'select', 'check', 'uncheck', 'scroll', 'scroll_into_view', 'upload', 'downloads', 'dialog', 'bind_text', 'webmcp_tools', 'webmcp_call', 'webmcp_adapter'];
function capabilities() { return { browser_control: config.browserControl || 'tools', playwright_version: version, operations: config.browserControl === 'native' ? [] : operations, native_connected: nativeConnected, target_ready: config.browserControl === 'native' ? nativeReady : !!page, remote_browser: !!config.remoteCdpUrl, webmcp: { enabled: !!config.webmcp, api: 'document.modelContext', adapters: [...adapters.keys()] } }; }
function locatorFor(root: Page | Frame | ReturnType<Page['frameLocator']>, selector: string, options: any): Locator | undefined {
    const target = options.target;
    if (target) {
        if (target.kind === 'role')
            return root.getByRole(target.role, { name: target.name, exact: target.exact ?? true });
        if (target.kind === 'text')
            return root.getByText(target.text, { exact: target.exact ?? true });
        if (target.kind === 'test_id')
            return root.getByTestId(target.value);
        if (target.kind === 'css')
            return root.locator(target.value);
        fail('invalid_target');
    }
    return selector ? root.locator(selector) : undefined;
}
async function command(req: BrowserCommand) {
    const options = JSON.parse(req.optionsJson || '{}');
    if (req.operation === 'capabilities')
        return capabilities();
    if (req.operation === 'native_descriptor') {
        if (!gateway)
            fail('native_disabled');
        return { endpoint: gateway.endpoint, headers: gateway.headers, playwright_version: version, now_us: now(), target_url: config.targetUrl, binding, observer_script: config.observerScript, webmcp: !!config.webmcp, webmcp_list_script: webmcp.listSource, webmcp_call_script: webmcp.callSource, context_options: { viewport: { width: 1280, height: 720 }, storage_state: config.storageStateJson ? JSON.parse(config.storageStateJson) : undefined }, permission_origins: config.permissionOrigins, permissions: [...(config.audio ? ['microphone'] : []), ...(config.camera ? ['camera'] : [])] };
    }
    if (req.operation === 'native_observe') {
        if (config.browserControl !== 'native' || !nativeConnected || fault)
            fail('native_not_connected');
        const rows = options.rows;
        if (!Array.isArray(rows) || rows.length > 256)
            fail('invalid_observations');
        const allowed = ['text.revision', 'text.removed', 'browser.page_created', 'browser.page_closed', 'browser.navigation', 'browser.selected', 'browser.page_error', 'capture.gap', 'target.ready', 'webmcp.tools_changed', 'webmcp.invoked', 'webmcp.responded'];
        for (const row of rows) {
            if (!allowed.includes(row.type) || !row.data || typeof row.data !== 'object' || !Number.isFinite(row.observed_at_us) || row.observed_at_us < 0)
                fail('invalid_observations');
            if (row.type === 'target.ready' || row.type === 'browser.selected') {
                nativeReady = true;
                if (attachTimer)
                    clearTimeout(attachTimer);
            }
            if (row.type === 'browser.page_closed' && row.data.selected)
                nativeReady = false;
            emit(row.type, { ...row.data, provenance: 'native_helper' }, Math.min(now(), row.observed_at_us));
        }
        return {};
    }
    if (fault)
        fail('browser_failed', fault);
    if (config.browserControl === 'native')
        fail('browser_control_external', 'Use the native Playwright Page for browser operations');
    if (req.operation === 'pages')
        return { pages: await Promise.all([...pages].map(async ([id, p]) => ({ page_id: id, url: p.url(), title: await p.title(), selected: p === page }))) };
    if (req.operation === 'new_page') {
        page = await context!.newPage();
        track(page);
        binding = {};
        if (req.value)
            await page.goto(req.value, { timeout: req.timeoutMs || 15000 });
        return { page_id: pageIds.get(page) };
    }
    const p = options.page_id ? pages.get(options.page_id) : page;
    if (!p || p.isClosed())
        fail('page_not_found');
    const selectedFrame = options.frame_id ? p.frames().find(f => frameId(f) === options.frame_id) : undefined;
    if (options.frame_id && !selectedFrame)
        fail('frame_not_found');
    if (options.expected_document_id && options.expected_document_id !== documentId(selectedFrame || p.mainFrame()))
        fail('stale_document');
    const timeout = req.timeoutMs || 15000;
    const root = selectedFrame || surface(p, req.frameSelector || (p === page ? binding.frame_selector : ''));
    const locator = locatorFor(root, req.selector, options);
    const required = () => locator || fail('target_required');
    switch (req.operation) {
        case 'open':
            await p.goto(req.value, { timeout });
            return { url: p.url() };
        case 'select_page':
            if (page !== p)
                binding = {};
            page = p;
            await p.bringToFront();
            return { page_id: pageIds.get(p) };
        case 'close_page':
            await p.close();
            break;
        case 'back':
            await p.goBack({ timeout });
            break;
        case 'forward':
            await p.goForward({ timeout });
            break;
        case 'reload':
            await p.reload({ timeout });
            break;
        case 'click':
            await required().click({ timeout });
            break;
        case 'double_click':
            await required().dblclick({ timeout });
            break;
        case 'hover':
            await required().hover({ timeout });
            break;
        case 'drag':
            await required().dragTo(locatorFor(root, options.destination || '', { target: options.destination_target }) || fail('destination_required'), { timeout });
            break;
        case 'click_at': {
            const { x, y } = options, viewport = p.viewportSize();
            if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || y < 0 || (viewport && (x >= viewport.width || y >= viewport.height)))
                fail('invalid_coordinates');
            await p.mouse.click(x, y);
            break;
        }
        case 'fill':
            await required().fill(req.value, { timeout });
            break;
        case 'type':
            await required().pressSequentially(req.value, { timeout });
            break;
        case 'press':
            if (locator)
                await locator.press(req.value, { timeout });
            else
                await p.keyboard.press(req.value);
            break;
        case 'select':
            await required().selectOption(options.values || req.value, { timeout });
            break;
        case 'check':
            await required().check({ timeout });
            break;
        case 'uncheck':
            await required().uncheck({ timeout });
            break;
        case 'wait':
            await required().waitFor({ state: options.state || 'visible', timeout });
            break;
        case 'scroll':
            if (!Number.isFinite(options.x || 0) || !Number.isFinite(options.y || 0))
                fail('invalid_coordinates');
            await p.mouse.wheel(options.x || 0, options.y || 0);
            break;
        case 'scroll_into_view':
            await required().scrollIntoViewIfNeeded({ timeout });
            break;
        case 'upload': {
            if (!/^[a-f0-9]{32}$/.test(req.value))
                fail('invalid_asset');
            await required().setInputFiles(resolve(config.artifactRoot, 'assets', req.value), { timeout });
            break;
        }
        case 'downloads': return { downloads };
        case 'dialog':
            dialogPolicies.set(p, { accept: !!options.accept, prompt: options.prompt });
            break;
        case 'bind_text':
            if (page !== p)
                page = p;
            binding = options.binding || {};
            await observe();
            return { binding };
        case 'snapshot': return { page_id: pageIds.get(p), document_id: documentId(p.mainFrame()), url: p.url(), title: await p.title(), viewport: p.viewportSize(), observed_at_us: now(), accessibility: await p.locator('body').ariaSnapshot({ timeout }), frames: p.frames().map(f => ({ frame_id: frameId(f), document_id: documentId(f), url: f.url(), name: f.name() })) };
        case 'text': {
            if (!binding.input_selector)
                fail('binding_required');
            const input = root.locator(binding.input_selector);
            await input.fill(req.value, { timeout });
            if (binding.submit_selector)
                await root.locator(binding.submit_selector).click({ timeout });
            else
                await input.press('Enter', { timeout });
            if (binding.acknowledgement_selector)
                await root.locator(binding.acknowledgement_selector).filter({ hasText: req.value }).last().waitFor({ timeout });
            else {
                const deadline = Date.now() + timeout;
                while (true) {
                    const value = await input.evaluate((el: any) => 'value' in el ? el.value : el.innerText);
                    if (!value.trim())
                        break;
                    if (Date.now() > deadline)
                        fail('text_unacknowledged');
                    await new Promise(resolve => setTimeout(resolve, 50));
                }
            }
            break;
        }
        case 'screenshot': {
            const path = 'frames/checkpoint-' + randomUUID() + '.png';
            await mkdir(join(config.artifactRoot, 'frames'), { recursive: true });
            await p.screenshot({ path: join(config.artifactRoot, path), timeout });
            return { path, page_id: pageIds.get(p), document_id: documentId(p.mainFrame()) };
        }
        case 'leave':
            if (binding.leave_selector)
                await root.locator(binding.leave_selector).click({ timeout: 2000 });
            break;
        case 'webmcp_adapter': {
            if (options.remove) {
                adapters.delete(String(options.remove));
                return { adapters: [...adapters.keys()] };
            }
            const adapter = webmcp.validateAdapter(options.adapter, fail);
            adapters.set(adapter.name, adapter);
            return { adapter: adapter.name, tools: adapter.tools.map(t => t.name), adapters: [...adapters.keys()] };
        }
        case 'webmcp_tools': {
            const where = { page_id: pageIds.get(p), frame_id: frameId(p.mainFrame()), document_id: documentId(p.mainFrame()), frame_url: p.url() };
            // The flag only turns Chromium's feature on; origin-trial sites expose tools without it.
            const listing = await webmcp.listTools(p, webmcpIds);
            return { ...listing, flag_enabled: !!config.webmcp, tools: [...listing.tools, ...webmcp.testerTools(adapters, p.url(), where)] };
        }
        case 'webmcp_call': {
            const source = options.source || '';
            const found = source === 'site' ? undefined : webmcp.findTester(adapters, req.value, p.url());
            if (found) {
                const where = { page_id: pageIds.get(p) };
                // Steps call command() directly: this call already holds the mutation queue.
                const run = (step: webmcp.Step) => command({ operation: step.operation, selector: step.selector || '', value: step.value || '', timeoutMs: step.timeout_ms || timeout, frameSelector: step.frame_selector || '', optionsJson: JSON.stringify({ page_id: pageIds.get(p), ...(step.options || {}) }) });
                return webmcp.callTester(found.adapter, found.tool, options.arguments, run, emit, where, fail);
            }
            if (source === 'tester')
                fail('webmcp_tool_not_found', `No site adapter tool named ${req.value} for this page`);
            return webmcp.callTool(p, selectedFrame, req.value, options.arguments, req.timeoutMs || 30000, webmcpIds, fail, webmcpCalls);
        }
        case 'calibration': {
            if (binding.kind !== 'harness')
                fail('not_calibration');
            const value = req.value ? JSON.parse(req.value) : undefined;
            if (value)
                await p.evaluate(c => { const h = (window as any).harness; if (c.kind === 'tone')
                    h.tone(c.frequency, c.duration_ms, c.delay_ms);
                else
                    h.visual(c.marker_id, c.color); }, value);
            const before = now();
            const data = await p.evaluate(() => (window as any).harness.snapshot());
            const after = now();
            return { ...data, clock_offset_us: (before + after) / 2 - data.now * 1000, clock_uncertainty_us: (after - before) / 2 };
        }
        default: fail('unsupported_operation');
    }
    return {};
}
const readOperations = new Set(['capabilities', 'native_descriptor', 'native_observe', 'snapshot', 'pages', 'screenshot', 'wait', 'downloads', 'webmcp_tools']);
const webmcpIds = { page: (p: Page) => pageIds.get(p), frame: frameId, document: documentId };
function dispatch(req: BrowserCommand) {
    if (readOperations.has(req.operation))
        return command(req);
    const task = mutationTail.then(() => command(req));
    mutationTail = task.catch(() => { });
    return task;
}
const unary = (fn: (req: any) => Promise<any>) => (call: any, callback: any) => { fn(call.request).then(value => callback(null, value), error => callback({ code: grpc.status.FAILED_PRECONDITION, details: String(error) })); };
let shutdownTask: Promise<void> | undefined;
function stop() {
    if (shutdownTask)
        return shutdownTask;
    shutdownTask = (async () => {
        stopping = true;
        if (polling)
            clearInterval(polling);
        if (attachTimer)
            clearTimeout(attachTimer);
        console.error('shutdown: gateway');
        await gateway?.close();
        console.error('shutdown: browser');
        if (browserServer) {
            // Chromium can keep a headed process alive after its remote contexts close.
            // Bound graceful shutdown, then reap our own browser process before devices stop.
            let timer: NodeJS.Timeout | undefined;
            const graceful = browserServer.close();
            const done = await Promise.race([
                graceful.then(() => true, () => false),
                new Promise<boolean>(resolve => { timer = setTimeout(() => resolve(false), 2000); }),
            ]);
            if (timer)
                clearTimeout(timer);
            if (!done) {
                emit('browser.shutdown_forced', { reason: 'graceful_close_timeout', grace_ms: 2000 });
                console.error('shutdown: forcing owned browser process', browserServer.process().exitCode, browserServer.process().signalCode);
                await browserServer.kill();
            }
        }
        await browser?.close();
        console.error('shutdown: completed');
        for (const call of listeners.keys())
            call.end();
        listeners.clear();
    })();
    return shutdownTask;
}
const service: BrowserServer = {
    start: unary(async (req: BrowserStart) => {
        if (req.protocolVersion !== 1)
            fail('incompatible_protocol_version', 'incompatible protocol version');
        if (browser || browserServer)
            fail('already_started');
        config = req;
        binding = req.bindingJson ? JSON.parse(req.bindingJson) : {};
        // A remote CDP browser (for example Browserbase) is text-only: its devices are not ours.
        const remote = !!req.remoteCdpUrl;
        if (remote && req.browserControl === 'native')
            fail('remote_browser_unsupported', 'A remote CDP browser supports tools mode only');
        if (!remote) {
            const launch = { headless: req.headless, chromiumSandbox: true, args: ['--window-size=1280,720', '--window-position=0,0', ...webmcp.launchArgs(req.webmcp)] };
            browserServer = await chromium.launchServer({ ...launch, host: '127.0.0.1' });
            const ownedProcess = browserServer.process();
            ownedProcess.once('exit', (code, signal) => {
                console.error('owned browser exited', code, signal);
                // Node emits 'close' only after all stdio pipes close. A sandboxed
                // Chromium descendant may retain a pipe after the browser exits.
                // Once the owned process has exited, no protocol traffic is valid.
                for (const stream of ownedProcess.stdio) stream?.destroy();
            });
            browserServer.on('close', () => { if (!stopping) {
                fault = 'browser_disconnected';
                emit('browser.error', { reason: fault });
            } });
        }
        if (req.browserControl === 'native') {
            gateway = await nativeGateway(browserServer!.wsEndpoint(), () => { nativeConnected = true; emit('browser.native_connected', {}); }, () => { nativeConnected = false; nativeReady = false; fault = 'native_client_disconnected'; emit('browser.error', { reason: fault }); });
            attachTimer = setTimeout(() => { fault = 'native_attach_timeout'; emit('browser.error', { reason: fault }); }, req.nativeAttachTimeoutMs || 120000);
            emit('browser.awaiting_client', {});
        }
        else {
            // Tools mode connects its own controller to the same owned browser server.
            // The shared process-exit hook also bounds headed text-only shutdown.
            browser = remote
                ? await chromium.connectOverCDP(req.remoteCdpUrl, { headers: req.remoteCdpHeadersJson ? JSON.parse(req.remoteCdpHeadersJson) : undefined, timeout: 30000 })
                : await chromium.connect(browserServer!.wsEndpoint());
            browser.on('disconnected', () => { if (!stopping) {
                fault = 'browser_disconnected';
                emit('browser.error', { reason: fault });
            } });
            // Remote services usually expose one preconfigured context; reuse it unless the
            // caller supplies storage state, which requires a new context.
            context = remote && !req.storageStateJson && browser.contexts().length
                ? browser.contexts()[0]
                : await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: req.storageStateJson ? JSON.parse(req.storageStateJson) : undefined, acceptDownloads: !remote });
            context.on('page', track);
            context.pages().forEach(track);
            const permissions = [...(req.audio ? ['microphone'] : []), ...(req.camera ? ['camera'] : [])];
            for (const origin of req.permissionOrigins)
                if (permissions.length)
                    await context.grantPermissions(permissions, { origin });
            if (req.harnessHtml)
                await context.route('http://127.0.0.1/mba-harness', route => route.fulfill({ body: req.harnessHtml, contentType: 'text/html' }));
            if (req.observerScript && binding.message_selector)
                await context.addInitScript({ content: req.observerScript.replace('__MBA_CONFIG__', () => JSON.stringify(binding)) });
            page = (remote && context.pages()[0]) || await context.newPage();
            track(page);
            if (req.targetUrl)
                await page.goto(req.targetUrl);
            if (binding.join_selector)
                await surface(page, binding.frame_selector).locator(binding.join_selector).click();
            if (binding.ready_selector)
                await surface(page, binding.frame_selector).locator(binding.ready_selector).waitFor({ state: 'visible' });
            if (req.calibrate)
                emit('devices.verified', { browser_settings: await page.evaluate(() => (window as any).harness.settings) });
            polling = setInterval(() => void observe(), 50);
            emit('target.ready', { url: page.url(), browser_version: browser.version(), remote_browser: remote });
        }
        return { protocolVersion: 1, nowUs: now(), environment: {}, capabilities: ['text', 'browser', 'screenshot'] };
    }),
    command: unary(async (req) => ({ json: JSON.stringify(await dispatch(req)), data: Buffer.alloc(0) })),
    observe(call) {
        const listener = { backlog: pending.splice(0), blocked: false };
        listeners.set(call, listener);
        call.on('cancelled', () => listeners.delete(call));
        call.on('close', () => listeners.delete(call));
        deliver(call, listener);
    },
    health: unary(async () => { if (fault)
        fail('browser_failed', fault); return { protocolVersion: 1, nowUs: now(), environment: {}, capabilities: [] }; }),
    stop: unary(async () => { await stop(); return {}; }),
};
const socket = process.argv[2];
if (!socket)
    throw new Error('usage: node main.js SOCKET');
const server = new grpc.Server({ 'grpc.max_receive_message_length': 8 * 1024 * 1024 });
server.addService(BrowserService, service);
server.bindAsync('unix:' + socket, grpc.ServerCredentials.createInsecure(), async (error) => { if (error)
    throw error; await chmod(socket, 0o600); });
async function shutdown() { await stop(); server.forceShutdown(); process.exit(0); }
process.on('SIGTERM', () => void shutdown());
process.on('SIGINT', () => void shutdown());
