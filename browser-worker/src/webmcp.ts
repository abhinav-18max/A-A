import type { CDPSession, Frame, Page } from 'playwright';
import { createHash } from 'node:crypto';

// WebMCP is Chromium's page-registered tool API (document.modelContext). It is off by default.
// Playwright already passes --enable-features=CDPScreenshotNewSurface and Chromium keeps only the
// last copy of a repeated switch, so the feature list is extended rather than appended.
export function launchArgs(enabled: boolean): string[] {
    if (!enabled)
        return [];
    const features = process.env.PLAYWRIGHT_LEGACY_SCREENSHOT ? ['WebMCP'] : ['CDPScreenshotNewSurface', 'WebMCP'];
    return ['--enable-features=' + features.join(',')];
}

export const OUTPUT_LIMIT = 256 * 1024;
const PREVIEW_LIMIT = 4096;
const FRAME_TIMEOUT_MS = 5000;

// Page functions are self-contained source so the native helpers can evaluate the same code.
// Chromium 153 exposes getTools() and executeTool(tool, inputJson); older builds used navigator.
export const listSource = `async () => {
  const mc = document.modelContext ?? navigator.modelContext;
  if (!mc || typeof mc.getTools !== 'function') return { available: false, tools: [] };
  const tools = await mc.getTools();
  return { available: true, tools: tools.filter(t => !('window' in t) || t.window === window).map(t => {
    let schema = t.inputSchema;
    if (typeof schema === 'string') { try { schema = JSON.parse(schema); } catch { schema = null; } }
    return { name: String(t.name), title: t.title ? String(t.title) : '', description: String(t.description ?? ''),
      input_schema: schema ?? null, origin: t.origin ? String(t.origin) : location.origin };
  }) };
}`;

export const callSource = `async ({ name, inputJson }) => {
  const mc = document.modelContext ?? navigator.modelContext;
  if (!mc || typeof mc.getTools !== 'function') return { status: 'unavailable' };
  const tool = (await mc.getTools()).find(t => t.name === name && (!('window' in t) || t.window === window));
  if (!tool) return { status: 'not_found' };
  try {
    const output = typeof mc.executeTool === 'function'
      ? await mc.executeTool(tool, inputJson)
      : await mc.invokeTool(name, JSON.parse(inputJson));
    return { status: 'completed', output: typeof output === 'string' ? output : JSON.stringify(output ?? null) };
  } catch (error) {
    return { status: 'error', error: String(error && error.message || error) };
  }
}`;

type Fail = (code: string, message?: string) => never;
type Emit = (type: string, data: unknown) => void;
type Ids = { page: (p: Page) => string | undefined, frame: (f: Frame) => string, document: (f: Frame) => string };
// Calls made by the adapter, so observed invocations can be attributed. Page code calling the same
// tool while an adapter call is in flight may be attributed to the adapter.
export type Pending = { name: string, until: number }[];

function claim(calls: Pending, name: string, entry?: { name: string, until: number }) {
    const now = Date.now();
    for (let i = calls.length - 1; i >= 0; i--)
        if (calls[i].until < now)
            calls.splice(i, 1);
    const index = entry ? calls.indexOf(entry) : calls.findIndex(c => c.name === name);
    if (index < 0)
        return false;
    calls.splice(index, 1);
    return true;
}

function timeout<T>(promise: Promise<T>, ms: number): Promise<T | 'timeout'> {
    let timer: NodeJS.Timeout | undefined;
    return Promise.race([promise, new Promise<'timeout'>(resolve => { timer = setTimeout(() => resolve('timeout'), ms); })])
        .finally(() => clearTimeout(timer));
}

function digest(text: string) { return createHash('sha256').update(text).digest('hex'); }

export async function listTools(p: Page, ids: Ids) {
    const tools: Record<string, unknown>[] = [], blocked: Record<string, string>[] = [];
    let available = false;
    await Promise.all(p.frames().map(async frame => {
        const where = { page_id: ids.page(p) || '', frame_id: ids.frame(frame), document_id: ids.document(frame), frame_url: frame.url() };
        let listing: { available: boolean, tools: Record<string, unknown>[] } | 'timeout';
        try {
            listing = await timeout(frame.evaluate(`(${listSource})()`) as Promise<any>, FRAME_TIMEOUT_MS);
        }
        catch (error) {
            // Cross-origin frames need allow="tools"; detached frames simply disappear.
            if (/permissions policy|NotAllowedError/.test(String(error)))
                blocked.push({ ...where, reason: 'permissions_policy' });
            return;
        }
        if (listing === 'timeout') {
            blocked.push({ ...where, reason: 'timeout' });
            return;
        }
        available ||= listing.available;
        for (const tool of listing.tools)
            tools.push({ ...tool, ...where, tool_source: 'site' });
    }));
    return { page_id: ids.page(p), document_id: ids.document(p.mainFrame()), available, tools, blocked_frames: blocked };
}

export async function callTool(p: Page, frame: Frame | undefined, name: string, args: unknown, timeoutMs: number, ids: Ids, fail: Fail, calls: Pending) {
    if (!name)
        fail('webmcp_tool_required', 'Supply the WebMCP tool name as value');
    if (frame === undefined) {
        const { tools } = await listTools(p, ids);
        const frames = [...new Set(tools.filter(t => t.name === name).map(t => t.frame_id as string))];
        if (frames.length > 1)
            fail('webmcp_ambiguous_tool', `Tool ${name} is registered in frames ${frames.join(', ')}; pass frame_id`);
        frame = frames.length ? p.frames().find(f => ids.frame(f) === frames[0]) : p.mainFrame();
        if (!frame)
            fail('frame_not_found');
    }
    const pending = { name, until: Date.now() + timeoutMs + 5000 };
    calls.push(pending);
    let result: { status: string, output?: string, error?: string } | 'timeout';
    try {
        result = await timeout(frame.evaluate(`(${callSource})(${JSON.stringify({ name, inputJson: JSON.stringify(args ?? {}) })})`) as Promise<any>, timeoutMs);
    }
    catch (error) {
        if (/Execution context was destroyed|navigat/i.test(String(error)))
            return { tool: name, status: 'navigated', untrusted: true, tool_source: 'site' };
        throw error;
    }
    if (result === 'timeout' || result.status === 'unavailable' || result.status === 'not_found')
        claim(calls, name, pending);
    if (result === 'timeout')
        fail('webmcp_timeout', `Tool ${name} did not respond within ${timeoutMs} ms`);
    if (result.status === 'unavailable')
        fail('webmcp_unavailable', 'The page does not expose document.modelContext; start the session with webmcp enabled unless the site is in Chrome\'s WebMCP origin trial');
    if (result.status === 'not_found')
        fail('webmcp_tool_not_found', `No WebMCP tool named ${name} in this frame`);
    const where = { page_id: ids.page(p), frame_id: ids.frame(frame), document_id: ids.document(frame) };
    if (result.status === 'error')
        return { tool: name, ...where, status: 'error', error: result.error, untrusted: true, tool_source: 'site' };
    const output = result.output ?? 'null', truncated = output.length > OUTPUT_LIMIT;
    let parsed: unknown = null;
    if (!truncated)
        try {
            parsed = JSON.parse(output);
        }
        catch {
            parsed = output;
        }
    return {
        tool: name, ...where, status: 'completed', result: parsed, output_text: truncated ? output.slice(0, OUTPUT_LIMIT) : undefined,
        truncated, output_bytes: Buffer.byteLength(output), output_sha256: digest(output),
        is_error: !!parsed && typeof parsed === 'object' && (parsed as any).isError === true,
        untrusted: true, tool_source: 'site',
    };
}

function preview(value: unknown) {
    const text = typeof value === 'string' ? value : JSON.stringify(value ?? null);
    return { preview: text.slice(0, PREVIEW_LIMIT), bytes: Buffer.byteLength(text), sha256: digest(text) };
}

// Journals tool registration and every invocation, including ones made by the page's own code.
export async function observe(p: Page, pageId: string, emit: Emit, calls: Pending) {
    const frames = new Map<Frame, CDPSession>();
    const invocations = new Map<string, string>();
    const attach = async (target: Page | Frame) => {
        let cdp: CDPSession;
        try {
            cdp = await p.context().newCDPSession(target);
        }
        catch {
            return; // Same-process frames share the page session.
        }
        if (target !== p) {
            // A cross-origin navigation moves the frame to a new target; replace its session.
            const previous = frames.get(target as Frame);
            frames.set(target as Frame, cdp);
            await previous?.detach().catch(() => { });
        }
        const summary = (t: any) => ({ name: t.name, description: String(t.description ?? '').slice(0, 500), annotations: t.annotations ?? {}, cdp_frame_id: t.frameId });
        cdp.on('WebMCP.toolsAdded', (e: any) => emit('webmcp.tools_changed', { page_id: pageId, change: 'added', tools: e.tools.map(summary) }));
        cdp.on('WebMCP.toolsRemoved', (e: any) => emit('webmcp.tools_changed', { page_id: pageId, change: 'removed', tools: e.tools.map((t: any) => ({ name: t.name, cdp_frame_id: t.frameId })) }));
        cdp.on('WebMCP.toolInvoked', (e: any) => {
            invocations.set(e.invocationId, e.toolName);
            emit('webmcp.invoked', { page_id: pageId, tool: e.toolName, invocation_id: e.invocationId, cdp_frame_id: e.frameId, initiator: claim(calls, e.toolName) ? 'adapter' : 'page', tool_source: 'site', input: preview(e.input), untrusted: true });
        });
        cdp.on('WebMCP.toolResponded', (e: any) => {
            // DevTools reports only the invocation ID here; name the tool so events stand alone.
            const tool = invocations.get(e.invocationId);
            invocations.delete(e.invocationId);
            emit('webmcp.responded', { page_id: pageId, tool, invocation_id: e.invocationId, tool_source: 'site', status: e.status, error: e.errorText, ...(e.output === undefined ? {} : { output: preview(e.output) }), untrusted: true });
        });
        await cdp.send('WebMCP.enable' as any).catch(() => { });
    };
    await attach(p);
    // Out-of-process iframes appear after navigation and need their own session.
    p.on('framenavigated', frame => { if (frame !== p.mainFrame()) void attach(frame); });
    p.on('framedetached', frame => { void frames.get(frame)?.detach().catch(() => { }); frames.delete(frame); });
}

// Site adapters: tester-defined tools for pages without WebMCP. Each tool is a list of ordinary
// browser operations run with real Playwright input; string fields may use {{argument}} placeholders.
export const STEP_OPERATIONS = new Set(['open', 'click', 'fill', 'press', 'wait', 'screenshot', 'snapshot', 'pages', 'back', 'forward', 'reload', 'double_click', 'hover', 'drag', 'click_at', 'type', 'select', 'check', 'uncheck', 'scroll', 'scroll_into_view', 'upload', 'downloads', 'dialog']);
const NAME = /^[A-Za-z0-9_.-]{1,64}$/;
const PLACEHOLDER = /\{\{\s*([A-Za-z0-9_]+)\s*\}\}/g;

export type Step = { operation: string, selector?: string, value?: string, timeout_ms?: number, frame_selector?: string, options?: Record<string, unknown> };
export type TesterTool = { name: string, description: string, input_schema: Record<string, any>, steps: Step[], read_only: boolean };
export type SiteAdapter = { name: string, description: string, url_pattern: string, tools: TesterTool[] };

export function validateAdapter(input: any, fail: Fail): SiteAdapter {
    const invalid = (message: string): never => fail('invalid_adapter', message);
    if (!input || typeof input !== 'object' || !NAME.test(input.name ?? ''))
        invalid('adapter.name must match ' + NAME);
    if (!Array.isArray(input.tools) || !input.tools.length || input.tools.length > 64)
        invalid('adapter.tools must list 1-64 tools');
    const names = new Set<string>();
    const tools = input.tools.map((tool: any): TesterTool => {
        if (!tool || !NAME.test(tool.name ?? '') || names.has(tool.name))
            invalid('tool names must be unique and match ' + NAME);
        names.add(tool.name);
        if (!Array.isArray(tool.steps) || !tool.steps.length || tool.steps.length > 50)
            invalid(`${tool.name}: steps must list 1-50 operations`);
        for (const step of tool.steps)
            if (!step || !STEP_OPERATIONS.has(step.operation))
                invalid(`${tool.name}: unsupported step operation ${step?.operation}`);
        const schema = tool.input_schema ?? { type: 'object', properties: {} };
        if (typeof schema !== 'object' || Array.isArray(schema))
            invalid(`${tool.name}: input_schema must be an object`);
        return { name: tool.name, description: String(tool.description ?? '').slice(0, 2000), input_schema: schema, steps: tool.steps, read_only: !!tool.read_only };
    });
    return { name: input.name, description: String(input.description ?? ''), url_pattern: String(input.url_pattern ?? ''), tools };
}

export function urlMatches(pattern: string, url: string) {
    if (!pattern)
        return true;
    const source = pattern.split('*').map(part => part.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*');
    return new RegExp('^' + source + '$').test(url);
}

export function render(value: unknown, args: Record<string, unknown>, fail: Fail): unknown {
    if (typeof value === 'string') {
        const whole = /^\{\{\s*([A-Za-z0-9_]+)\s*\}\}$/.exec(value);
        if (whole) {
            // A lone placeholder keeps the argument's type, e.g. numbers for click_at coordinates.
            if (!(whole[1] in args))
                fail('webmcp_missing_argument', 'Missing argument ' + whole[1]);
            return args[whole[1]];
        }
        return value.replace(PLACEHOLDER, (_, name) => {
            if (!(name in args))
                fail('webmcp_missing_argument', 'Missing argument ' + name);
            return String(args[name]);
        });
    }
    if (Array.isArray(value))
        return value.map(item => render(item, args, fail));
    if (value && typeof value === 'object')
        return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, render(v, args, fail)]));
    return value;
}

export function testerTools(adapters: Map<string, SiteAdapter>, url: string, where: Record<string, unknown>) {
    return [...adapters.values()].filter(adapter => urlMatches(adapter.url_pattern, url)).flatMap(adapter => adapter.tools.map(tool => ({
        name: tool.name, title: '', description: tool.description, input_schema: tool.input_schema, ...where,
        annotations: { readOnlyHint: tool.read_only }, adapter: adapter.name, tool_source: 'tester',
    })));
}

export function findTester(adapters: Map<string, SiteAdapter>, name: string, url: string) {
    for (const adapter of adapters.values())
        if (urlMatches(adapter.url_pattern, url))
            for (const tool of adapter.tools)
                if (tool.name === name)
                    return { adapter, tool };
    return undefined;
}

type Run = (step: Step) => Promise<unknown>;

export async function callTester(adapter: SiteAdapter, tool: TesterTool, args: unknown, run: Run, emit: Emit, where: Record<string, unknown>, fail: Fail) {
    const input = args && typeof args === 'object' && !Array.isArray(args) ? args as Record<string, unknown> : {};
    for (const name of Array.isArray(tool.input_schema.required) ? tool.input_schema.required : [])
        if (!(name in input))
            fail('webmcp_missing_argument', 'Missing argument ' + name);
    const invocation = createHash('sha256').update(adapter.name + tool.name + Date.now() + Math.random()).digest('hex').slice(0, 32);
    const common = { ...where, tool: tool.name, adapter: adapter.name, invocation_id: invocation, tool_source: 'tester' };
    emit('webmcp.invoked', { ...common, initiator: 'adapter', input: preview(input) });
    const steps: unknown[] = [];
    for (const [index, step] of tool.steps.entries()) {
        try {
            steps.push({ operation: step.operation, result: await run(render(step, input, fail) as Step) });
        }
        catch (error) {
            const message = String(error instanceof Error ? error.message : error);
            emit('webmcp.responded', { ...common, status: 'Error', error: message });
            fail('webmcp_step_failed', `${tool.name} step ${index} (${step.operation}): ${message}`);
        }
    }
    const result = { steps };
    emit('webmcp.responded', { ...common, status: 'Completed', output: preview(result) });
    return { tool: tool.name, adapter: adapter.name, ...where, status: 'completed', result, truncated: false, untrusted: true, tool_source: 'tester' };
}
