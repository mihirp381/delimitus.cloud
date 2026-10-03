/**
 * The test app of the browser isolation suite (SSC-029). Apps A (`alpha`) and B (`bravo`) are this
 * one program run twice; it learns its name from `X-Forwarded-Host`, which the gateway sets to the
 * app host. No dependencies besides the reconnect helper (SSC-090), read from the repository, or
 * from `./ssc-reconnect/` in a deployed copy (README).
 *
 *   GET  /                 a page naming the app and the person the identity note names
 *   GET  /whoami           the same as JSON, with the `Cookie` and deadline headers the app got
 *   POST /note?m=          recorded in the log, as is any GET with `m`; GET /log lists them
 *   GET  /set-cookies      answers with cookies named like the platform's, and one of its own
 *   GET  /toss?value=      answers with platform-named cookies for the parent domain
 *   GET  /cold/...         answers after 3 seconds, as a cold instance would
 *   GET  /ticks            an event stream of three events a second apart
 *   GET  /events           a counter over an event stream, ended before the deadline (helper)
 *   GET  /raw-events       an event stream that never ends by itself; its first event is /whoami
 *   GET  /reconnect        a page keeping a counter over sscSocket and EventSource
 *   WS   /echo?m=          recorded in the log; sends /whoami, then echoes
 *   WS   /cold-ws          accepted after 3 seconds
 *   WS   /ws?after=        a counter, closed with 1012 before the deadline (helper)
 *   WS   /raw-ws           sends /whoami, then ticks until something cuts it
 */

import { createHash } from 'node:crypto';
import { createServer } from 'node:http';

const reconnect = await import('./ssc-reconnect/index.js').catch(() => import('../../../helpers/node/ssc-reconnect/index.js'));
const { DEADLINE_HEADER, browserClient, closeBeforeDeadline, endBeforeDeadline } = reconnect;

const COLD_MS = 3000;
const TICK_MS = 500;
const LOG_MAX = 1000;
const GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const log = [];

/** Text safe to put in HTML. */
function escape(text) {
  return String(text).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

/** What the gateway told this app about one request. The identity note is decoded, not verified. */
function seen(req) {
  let sub = null;
  const note = req.headers['x-ssc-identity'];
  if (typeof note === 'string') {
    try {
      sub = JSON.parse(Buffer.from(note.split('.')[1], 'base64url').toString()).sub ?? null;
    } catch {
      sub = null;
    }
  }
  return {
    app: String(req.headers['x-forwarded-host'] ?? '').split('.')[0] || null,
    sub,
    cookie: req.headers.cookie ?? null,
    deadline: req.headers[DEADLINE_HEADER.toLowerCase()] ?? null,
  };
}

/** Add `kind` and the request's `m` mark to the log. */
function remember(kind, req) {
  log.push({ kind, m: new URL(req.url, 'http://app').searchParams.get('m'), at: Date.now() });
  if (log.length > LOG_MAX) log.shift();
}

function json(res, body, headers = {}) {
  res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store', ...headers });
  res.end(JSON.stringify(body));
}

function html(res, body) {
  res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
  res.end(`<!doctype html><meta charset=utf-8>${body}`);
}

function page(req, extra = '') {
  const me = seen(req);
  return `<title>${escape(me.app)}</title><h1 id=app>${escape(me.app)}</h1><p id=who>${escape(me.sub)}</p>${extra}`;
}

function reconnectPage() {
  return `<title>reconnect</title><script>${browserClient()}</script><script>
window.iso = { ws: { opens: 0, seen: [] }, es: { opens: 0, seen: [] } };
let last = -1;
sscSocket(() => \`wss://\${location.host}/ws?after=\${last}\`, {
  onopen() { iso.ws.opens += 1; },
  onmessage(event) { last = Number(event.data); iso.ws.seen.push(last); },
});
const events = new EventSource('/events');
events.onopen = () => { iso.es.opens += 1; };
events.onmessage = (event) => { iso.es.seen.push(Number(event.data)); };
</script>`;
}

function startStream(res) {
  res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store' });
  res.flushHeaders();
}

/** Call `send` every `ms` until `res` closes. */
function every(res, ms, send) {
  const timer = setInterval(() => {
    if (!res.writableEnded) send();
  }, ms);
  res.once('close', () => clearInterval(timer));
  return timer;
}

function ticks(res) {
  startStream(res);
  let n = 0;
  res.write(`data: ${n}\n\n`);
  const timer = every(res, 1000, () => {
    n += 1;
    res.write(`data: ${n}\n\n`);
    if (n === 2) clearInterval(timer);
  });
}

function events(req, res) {
  let n = Number(req.headers['last-event-id'] ?? -1);
  startStream(res);
  every(res, TICK_MS, () => {
    n += 1;
    res.write(`id: ${n}\ndata: ${n}\n\n`);
  });
  endBeforeDeadline(res, req.headers);
}

function rawEvents(req, res) {
  startStream(res);
  res.write(`data: ${JSON.stringify(seen(req))}\n\n`);
  every(res, 1000, () => res.write('data: tick\n\n'));
}

function frame(opcode, payload) {
  const head = payload.length < 126 ? Buffer.from([0x80 | opcode, payload.length]) : Buffer.from([0x80 | opcode, 126, payload.length >> 8, payload.length & 0xff]);
  return Buffer.concat([head, payload]);
}

/** One server-side WebSocket: text frames out and in, `close(code, reason)` and a `close` event.
 * A close it starts waits up to 2 seconds for the browser's close frame before it ends the
 * connection, as the protocol asks; ending at once makes WebKit report 1005 instead of the code. */
class Socket {
  #tcp;
  #buffer = Buffer.alloc(0);
  #listeners = [];
  #closing = false;
  closed = false;
  onText = null;

  constructor(tcp) {
    this.#tcp = tcp;
    tcp.on('data', (data) => this.#read(data));
    tcp.on('close', () => this.#finish());
  }

  #read(data) {
    this.#buffer = Buffer.concat([this.#buffer, data]);
    for (;;) {
      const b = this.#buffer;
      if (b.length < 2) return;
      let length = b[1] & 0x7f;
      let at = 2;
      if (length === 126) {
        if (b.length < 4) return;
        length = b.readUInt16BE(2);
        at = 4;
      } else if (length === 127) {
        if (b.length < 10) return;
        length = Number(b.readBigUInt64BE(2));
        at = 10;
      }
      const masked = (b[1] & 0x80) !== 0;
      const end = at + (masked ? 4 : 0) + length;
      if (b.length < end) return;
      const payload = Buffer.from(b.subarray(end - length, end));
      if (masked) {
        const mask = b.subarray(at, at + 4);
        for (let i = 0; i < payload.length; i += 1) payload[i] ^= mask[i % 4];
      }
      this.#buffer = b.subarray(end);
      const opcode = b[0] & 0x0f;
      if (opcode === 0x1 && this.onText) this.onText(payload.toString());
      else if (opcode === 0x8) {
        this.close(payload.length >= 2 ? payload.readUInt16BE(0) : 1000, '');
        this.#tcp.end();
      }
      else if (opcode === 0x9) this.#tcp.write(frame(0xa, payload));
    }
  }

  #finish() {
    if (this.closed) return;
    this.closed = true;
    for (const listener of this.#listeners) listener();
  }

  once(event, listener) {
    if (event === 'close') this.#listeners.push(listener);
  }

  send(text) {
    if (!this.#closing && !this.#tcp.destroyed) this.#tcp.write(frame(0x1, Buffer.from(text)));
  }

  close(code, reason) {
    if (this.#closing) return;
    this.#closing = true;
    const payload = Buffer.alloc(2 + Buffer.byteLength(reason));
    payload.writeUInt16BE(code, 0);
    payload.write(reason, 2);
    if (!this.#tcp.destroyed) this.#tcp.write(frame(0x8, payload));
    setTimeout(() => this.#tcp.destroy(), 2000).unref();
  }
}

function accept(req, tcp) {
  const key = createHash('sha1').update(`${req.headers['sec-websocket-key']}${GUID}`).digest('base64');
  tcp.write(`HTTP/1.1 101 Switching Protocols\r\nupgrade: websocket\r\nconnection: Upgrade\r\nsec-websocket-accept: ${key}\r\n\r\n`);
  return new Socket(tcp);
}

/** Send `text()` on `socket` every `ms` until it closes. */
function tick(socket, ms, text) {
  const timer = setInterval(() => socket.send(text()), ms);
  socket.once('close', () => clearInterval(timer));
}

const server = createServer((req, res) => {
  const { pathname: path, searchParams } = new URL(req.url, 'http://app');
  if (req.method === 'GET' && searchParams.has('m')) remember('get', req);
  if (path.startsWith('/cold/')) return void setTimeout(() => html(res, page(req, '<p id=awake>awake</p>')), COLD_MS);
  if (path === '/note' && req.method === 'POST') {
    remember('post', req);
    req.resume();
    return json(res, { ok: true });
  }
  if (path === '/whoami') return json(res, seen(req));
  if (path === '/log') return json(res, log);
  if (path === '/set-cookies') {
    const cookies = ['__Host-ssc-session=from-app; Path=/; Secure; HttpOnly', '__Secure-ssc-x=1; Path=/; Secure', '__host-SSC-y=1; Path=/; Secure', 'iso-app=1; Path=/; Secure; SameSite=Lax'];
    return json(res, { ok: true }, { 'set-cookie': cookies });
  }
  if (path === '/toss') {
    const value = new URL(req.url, 'http://app').searchParams.get('value') ?? 'x';
    const parent = String(req.headers['x-forwarded-host'] ?? '').split('.').slice(1).join('.');
    const cookies = [`__Host-ssc-session=${value}; Domain=${parent}; Path=/; Secure`, `__host-ssc-session=${value}; Domain=${parent}; Path=/; Secure`];
    return json(res, { ok: true }, { 'set-cookie': cookies });
  }
  if (path === '/ticks') return ticks(res);
  if (path === '/events') return events(req, res);
  if (path === '/raw-events') return rawEvents(req, res);
  if (path === '/reconnect') return html(res, reconnectPage());
  if (path === '/') return html(res, page(req));
  res.writeHead(404, { 'content-type': 'text/plain' });
  res.end('not here');
});

server.on('upgrade', (req, tcp) => {
  tcp.on('error', () => {});
  const url = new URL(req.url, 'http://app');
  if (url.pathname === '/cold-ws') {
    return void setTimeout(() => accept(req, tcp).send('awake'), COLD_MS);
  }
  if (url.pathname === '/echo') {
    remember('upgrade', req);
    const socket = accept(req, tcp);
    socket.send(JSON.stringify(seen(req)));
    socket.onText = (text) => socket.send(text);
    return;
  }
  if (url.pathname === '/ws') {
    const socket = accept(req, tcp);
    let n = Number(url.searchParams.get('after') ?? -1);
    tick(socket, TICK_MS, () => String((n += 1)));
    closeBeforeDeadline(socket, req.headers);
    return;
  }
  if (url.pathname === '/raw-ws') {
    const socket = accept(req, tcp);
    socket.send(JSON.stringify(seen(req)));
    tick(socket, 1000, () => 'tick');
    return;
  }
  tcp.end('HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\nconnection: close\r\n\r\n');
});

server.listen(Number(process.env.PORT ?? 8080), process.env.HOST ?? '0.0.0.0');
