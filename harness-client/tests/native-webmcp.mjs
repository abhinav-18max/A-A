// Native Playwright page plus A&A WebMCP helpers from TypeScript.
import assert from 'node:assert/strict';
import { Client } from '../index.js';

const client = new Client(process.env.MBA_URL, process.env.MBA_TOKEN);
const session = await client.start({ mode: 'text', binding: { kind: 'manual' }, browser_control: 'native', recording: false, webmcp: true });
const native = await session.connectNative();
try {
    await native.page.goto(process.env.TARGET_URL);
    let listing;
    for (let i = 0; i < 100 && !listing?.tools.length; i++) {
        listing = await native.webmcpTools(native.page);
        await new Promise(resolve => setTimeout(resolve, 50));
    }
    assert.ok(listing.tools.some(t => t.name === 'add_numbers'));
    const result = await native.webmcpCall(native.page, 'add_numbers', { a: 5, b: 6 });
    assert.equal(result.result.content[0].text, '11');
    assert.equal(result.untrusted, true);
    let observed = false;
    for (let i = 0; i < 50 && !observed; i++) {
        observed = (await session.eventPage()).some(e => e.type === 'webmcp.responded' && e.data.provenance === 'native_helper');
        await new Promise(resolve => setTimeout(resolve, 50));
    }
    assert.equal(observed, true);
}
finally {
    await native.close();
}
console.log('PASS');
