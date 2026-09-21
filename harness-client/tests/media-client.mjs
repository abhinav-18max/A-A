// Remote TypeScript client checks that do not need Linux media devices.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import { Client, decodeMediaPacket } from '../index.js';

const packet = decodeMediaPacket(readFileSync(process.env.PACKET));
const client = new Client(process.env.MBA_URL, process.env.MBA_TOKEN);
const session = await client.start({ mode: 'text', recording: false, webmcp: true, target_url: process.env.TARGET_URL });
const report = { packet: { streamId: packet.streamId, sequence: packet.sequence, ptsUs: packet.ptsUs, bytes: packet.data.length, format: packet.format, discontinuity: packet.discontinuity } };
try {
    let listing;
    for (let i = 0; i < 100; i++) {
        listing = await session.webmcp.tools();
        if (listing.tools.some(t => t.name === 'add_numbers'))
            break;
        await new Promise(resolve => setTimeout(resolve, 50));
    }
    report.sum = (await session.webmcp.call('add_numbers', { a: 40, b: 2 })).result.content[0].text;
    await session.command('open', { value: process.env.FORM_URL });
    await session.webmcp.addAdapter({ name: 'form', tools: [{ name: 'say', steps: [{ operation: 'fill', selector: '#composer', value: '{{text}}' }, { operation: 'click', selector: '#send' }] }] });
    report.tester = (await session.webmcp.call('say', { text: 'typed' }, { source: 'tester' })).status;
    assert.equal(await session.command('wait', { selector: '.assistant' }).then(() => 'ok'), 'ok');
    report.asset = await session.upload(process.env.UPLOAD);
    for await (const event of session.events()) {
        if (event.type === 'webmcp.invoked') {
            report.event = event.type;
            break;
        }
    }
    await assert.rejects(session.webmcp.call('missing'), error => error.code === 'webmcp_tool_not_found');
}
finally {
    await session.stop();
}
console.log(JSON.stringify(report));
