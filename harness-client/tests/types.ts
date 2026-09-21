import {Client} from '../index.js';
import type {Page, BrowserContext} from 'playwright';
async function example(client: Client) {
  const session = await client.start({browser_control: 'native', mode: 'text', binding: {kind: 'manual'}});
  await using native = await session.connectNative();
  const page: Page = native.page;
  const context: BrowserContext = native.context;
  await page.getByRole('button', {name: 'Start'}).dblclick();
  await native.registerContext(context);
}
void example;
