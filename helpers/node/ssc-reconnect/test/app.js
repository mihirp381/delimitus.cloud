/**
 * The test app for the reconnect helper: a counter over an event stream (`/events`) and over a
 * WebSocket (`/ws`), each resuming where the last connection stopped. In front of it a stand-in
 * for the gateway stamps every request with `X-SSC-Request-Deadline` `limit` seconds ahead, so a
 * test meets the limit in seconds instead of minutes. The WebSocket server is the least of
 * RFC 6455 a test needs, so the helper stays without dependencies.
 */

import { createHash } from 'node:crypto';
import { createServer } from 'node:http';

import { DEADLINE_HEADER, closeBeforeDeadline, endBeforeDeadline } from '../index.js';

const TICK_MS = 100;

function frame(opcode, payload) {
  const head = payload.length < 126 ? Buffer.from([0x80 | opcode, payload.length]) : Buffer.from([0x80 | opcode, 126, payload.length >> 8, payload.length & 0xff]);
  return Buffer.concat([head, payload]);
}

function closeFrame(code, reason) {
  const payload = Buffer.alloc(2 + Buffer.byteLength(reason));
  payload.writeUInt16BE(code, 0);
  payload.write(reason, 2);
  return frame(0x8, payload);
}

/** One server-side WebSocket: `send`, `close(code, reason)` and a `close` event, as `ws` has. */
class Socket {
  #tcp;
  #listeners = [];
  closed = false;
  closeCode = null;

  constructor(tcp) {
    this.#tcp = tcp;
    tcp.on('data', (data) => {
      for (let at = 0; at + 6 <= data.length; at += 6 + (data[at + 1] & 0x7f)) {
        if ((data[at] & 0x0f) !== 0x8) continue;
        const mask = data.subarray(at + 2, at + 6);
        const code = (data[at + 1] & 0x7f) >= 2 ? ((data[at + 6] ^ mask[0]) << 8) | (data[at + 7] ^ mask[1]) : 1005;
        this.#finish(code);
      }
    });
    tcp.on('close', () => this.#finish(1006));
    tcp.on('error', () => {});
  }

  #finish(code) {
    if (this.closed) return;
    this.closed = true;
    if (this.closeCode === null) {
      this.closeCode = code;
      if (!this.#tcp.destroyed) this.#tcp.write(closeFrame(code === 1005 ? 1000 : code, ''));
    }
    this.#tcp.end();
    for (const listener of this.#listeners) listener();
  }

  once(event, listener) {
    if (event === 'close') this.#listeners.push(listener);
  }

  send(text) {
    if (!this.closed) this.#tcp.write(frame(0x1, Buffer.from(text)));
  }

  close(code, reason) {
    if (this.closed || this.closeCode !== null) return;
    this.closeCode = code;
    this.#tcp.write(closeFrame(code, reason));
    setTimeout(() => this.#tcp.destroy(), 1000).unref();
  }

  drop() {
    this.#tcp.destroy();
  }

  /** A close frame without a code: what WebKit reports for every close the server starts. */
  closeBare() {
    if (this.closed || this.closeCode !== null) return;
    this.closeCode = 1005;
    this.#tcp.write(frame(0x8, Buffer.alloc(0)));
    setTimeout(() => this.#tcp.destroy(), 1000).unref();
  }
}

/**
 * Start the app on a free port. `limit` is seconds to each deadline (null: no header, as on a
 * laptop); `margin` and `retryMs` go to the helper; `ws` is `restart` (the helper), `drop` (the
 * first socket is cut without a close after 300 ms), `bare` (the first socket is closed without
 * a code after 300 ms), `done` (closed with 1000 after 300 ms) or `refuse` (every upgrade answered
 * 404).
 */
export async function startApp({ limit = 2, margin = 0.5, retryMs = 50, ws = 'restart' } = {}) {
  const connections = [];
  const upgraded = new Set();

  function stamp(req) {
    if (limit !== null) req.headers[DEADLINE_HEADER.toLowerCase()] = String(Math.floor(Date.now() / 1000) + limit);
    const seen = { deadline: limit === null ? null : Number(req.headers[DEADLINE_HEADER.toLowerCase()]), opened: Date.now(), ended: null, resume: null, closeCode: null };
    connections.push(seen);
    return seen;
  }

  const server = createServer((req, res) => {
    const seen = stamp(req);
    seen.resume = req.headers['last-event-id'] ?? null;
    let n = Number(seen.resume ?? -1);
    res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store' });
    const ticker = setInterval(() => {
      n += 1;
      res.write(`id: ${n}\ndata: ${n}\n\n`);
    }, TICK_MS);
    res.once('close', () => {
      clearInterval(ticker);
      seen.ended = Date.now();
    });
    endBeforeDeadline(res, req.headers, { margin, retryMs });
  });

  server.on('upgrade', (req, tcp) => {
    upgraded.add(tcp);
    tcp.once('close', () => upgraded.delete(tcp));
    const seen = stamp(req);
    if (ws === 'refuse') {
      tcp.end('HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\nconnection: close\r\n\r\n');
      return;
    }
    const accept = createHash('sha1').update(`${req.headers['sec-websocket-key']}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest('base64');
    tcp.write(`HTTP/1.1 101 Switching Protocols\r\nupgrade: websocket\r\nconnection: Upgrade\r\nsec-websocket-accept: ${accept}\r\n\r\n`);
    const socket = new Socket(tcp);
    seen.resume = new URL(req.url, 'http://app').searchParams.get('after');
    let n = Number(seen.resume ?? -1);
    const ticker = setInterval(() => {
      n += 1;
      socket.send(String(n));
    }, TICK_MS);
    socket.once('close', () => {
      clearInterval(ticker);
      seen.ended = Date.now();
      seen.closeCode = socket.closeCode;
    });
    if (ws === 'drop' && connections.length === 1) setTimeout(() => socket.drop(), 300);
    if (ws === 'bare' && connections.length === 1) setTimeout(() => socket.closeBare(), 300);
    if (ws === 'done') setTimeout(() => socket.close(1000, 'done'), 300);
    closeBeforeDeadline(socket, req.headers, { margin });
  });

  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  return {
    url: `http://127.0.0.1:${port}`,
    wsUrl: `ws://127.0.0.1:${port}/ws`,
    connections,
    close: () => {
      server.closeAllConnections();
      for (const tcp of upgraded) tcp.destroy();
      return new Promise((resolve) => server.close(resolve));
    },
  };
}
