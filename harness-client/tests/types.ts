import {Client, audioFormat, decodeMediaPacket} from '../index.js';
import type {Page, BrowserContext} from 'playwright';
async function example(client: Client) {
  const session = await client.start({browser_control: 'native', mode: 'text', binding: {kind: 'manual'}});
  await using native = await session.connectNative();
  const page: Page = native.page;
  const context: BrowserContext = native.context;
  await page.getByRole('button', {name: 'Start'}).dblclick();
  await native.registerContext(context);
  const listing = await native.webmcpTools(page);
  const called = await native.webmcpCall(page, listing.tools[0].name, {q: 'x'});
  const untrusted: true = called.untrusted;
  void untrusted;
}
async function media(client: Client, pcm: AsyncIterable<Uint8Array>) {
  const session = await client.start({mode: 'audio', camera: false});
  await using mic = await session.audio.openInput({rate: 24000});
  for await (const chunk of pcm)
    await mic.write(chunk);
  for await (const packet of session.audio.capture({channels: 1})) {
    const rate: number | undefined = packet.format.rate;
    void rate;
    break;
  }
  for await (const frame of session.visual.frames({fps: 1})) {
    void frame.data.length;
    break;
  }
  await (await session.sendAudio('question.wav')).wait();
  const files = await session.recording.stop();
  void files;
  for await (const event of session.events()) {
    if (event.type === 'webmcp.invoked')
      break;
  }
  const tools = await session.webmcp.tools();
  await session.webmcp.addAdapter({name: 'site', tools: [{name: 'start', steps: [{operation: 'click', selector: 'button.start'}]}]});
  await session.webmcp.call(tools.tools[0].name, {}, {source: 'tester'});
  void audioFormat({rate: 16000, channels: 1});
  void decodeMediaPacket(new Uint8Array());
}
void example;
void media;
