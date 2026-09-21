import { Client } from '../index.js';
import assert from 'node:assert/strict';
const client = new Client(process.env.MBA_URL, process.env.MBA_TOKEN);
const session = await client.start({ mode: 'text', binding: { kind: 'manual' }, browser_control: 'native', recording: false });
const native = await session.connectNative();
try {
    await native.page.setContent('<input><button onclick="const d=document.createElement(\'p\');d.className=\'assistant\';d.textContent=document.querySelector(\'input\').value;document.body.append(d)">Send</button><input type="file" id="upload">');
    await native.bindText(native.page, { message_selector: '[data-marker="semi;colon"]' });
    await native.bindText(native.page, { message_selector: '.assistant' });
    await native.page.getByRole('textbox').first().fill('TypeScript native');
    await native.page.getByRole('button', { name: 'Send' }).click();
    assert.equal(await native.page.locator('.assistant').innerText(), 'TypeScript native');
    await native.page.locator('#upload').setInputFiles({ name: 'fixture.txt', mimeType: 'text/plain', buffer: Buffer.from('payload') });
    assert.equal(await native.page.locator('#upload').evaluate(el => el.files[0].name), 'fixture.txt');
    const popup = await native.context.newPage();
    await popup.route('**/*', route => route.fulfill({ body: '<button>intercepted</button>', contentType: 'text/html' }));
    await popup.goto('https://fixture.invalid');
    assert.equal(await popup.getByRole('button').innerText(), 'intercepted');
    await native.selectPage(popup);
    assert.equal((await session.capabilities()).target_ready, true);
    let observed = false;
    for (let i = 0; i < 50; i++) {
        const events = await client.request(session.path + '/events');
        observed = events.some(event => event.type === 'text.revision' && event.data.content === 'TypeScript native');
        if (observed)
            break;
        await new Promise(resolve => setTimeout(resolve, 50));
    }
    assert.equal(observed, true);
}
finally {
    await native.close();
}
assert.equal((await client.request(session.path)).state, 'closed');
console.log('PASS: TypeScript native Playwright, upload, route interception, pages, observations, cleanup');
