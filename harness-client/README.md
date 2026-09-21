# @mba/harness-client

External native Playwright setup for an A&A worker. Requires Node.js 22+ and
Playwright 1.63.x (tested at 1.63.0). The helper returns raw Playwright objects.

```typescript
import {Client} from '@mba/harness-client';
const client = new Client(process.env.MBA_URL!, process.env.MBA_TOKEN!);
const session = await client.start({
  browser_control: 'native', mode: 'text', binding: {kind: 'manual'},
});
await using native = await session.connectNative();
await native.page.goto('https://your-target.example');
await native.page.getByRole('button', {name: 'Start'}).click();
```

Control mode is fixed for the session. One native connection owns its contexts;
disconnection ends the A&A session. `registerContext`, `selectPage`, and `bindText`
configure normalized observation forwarding. Additional registered contexts must
belong to the same browser. Keep model/provider connections outside the worker.

Audio and video use A&A's separate binary interfaces. This package supplies native
browser setup and session control, not a provider SDK or a second browser.
