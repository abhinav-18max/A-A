import { createServer } from 'node:http';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { WebSocket, WebSocketServer } from 'ws';
// This gateway authorizes connections; it never interprets the Playwright protocol.
export async function nativeGateway(upstream: string, onConnect: () => void, onClose: () => void) {
    const token = randomBytes(32).toString('hex');
    let admitted = false, closed = false;
    const peers = new Set<WebSocket>();
    const server = createServer((_, response) => { response.writeHead(404); response.end(); });
    const wss = new WebSocketServer({ noServer: true, maxPayload: 32 * 1024 * 1024 });
    server.on('upgrade', (request, socket, head) => {
        const actual = Buffer.from(request.headers.authorization || '');
        const expected = Buffer.from('Bearer ' + token);
        const authorized = actual.length === expected.length && timingSafeEqual(actual, expected);
        if (!authorized || request.url !== '/playwright' || admitted || closed) {
            socket.end('HTTP/1.1 ' + (authorized ? '409 Conflict' : '401 Unauthorized') + '\r\nConnection: close\r\n\r\n');
            return;
        }
        admitted = true;
        wss.handleUpgrade(request, socket, head, client => {
            const remote = new WebSocket(upstream, { maxPayload: 32 * 1024 * 1024 });
            peers.add(client);
            peers.add(remote);
            let finished = false;
            const finish = () => {
                if (finished)
                    return;
                finished = true;
                client.terminate();
                remote.terminate();
                peers.delete(client);
                peers.delete(remote);
                if (!closed)
                    onClose();
            };
            client.pause();
            remote.on('open', () => { onConnect(); client.resume(); });
            function forward(source: WebSocket, target: WebSocket) {
                source.on('message', (data, binary) => {
                    if (target.readyState !== WebSocket.OPEN || target.bufferedAmount > 16 * 1024 * 1024)
                        return finish();
                    source.pause();
                    target.send(data, { binary }, error => { if (error)
                        finish();
                    else
                        source.resume(); });
                });
            }
            forward(client, remote);
            forward(remote, client);
            client.on('error', finish);
            remote.on('error', finish);
            client.on('close', finish);
            remote.on('close', finish);
        });
    });
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
    const address = server.address() as {
        port: number;
    };
    return {
        endpoint: 'ws://127.0.0.1:' + address.port + '/playwright',
        headers: { Authorization: 'Bearer ' + token },
        close: async () => {
            closed = true;
            for (const peer of peers)
                peer.terminate();
            wss.close();
            await new Promise<void>(resolve => server.close(() => resolve()));
        },
    };
}
