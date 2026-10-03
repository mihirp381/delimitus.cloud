/**
 * Keep a WebSocket or an event stream going past the gateway's limit (SSC-090).
 *
 * Cloud Run ends every request at a fixed time, and an open WebSocket or event stream is one
 * request: after 5 minutes for most apps, 60 for a session app. The gateway tells the app that
 * time in `X-SSC-Request-Deadline` (Unix seconds). The
 * server cannot reconnect a browser, so these helpers end the stream cleanly a little before that
 * time and the browser opens a new one, which gets a new deadline:
 *
 *  - An event stream: call `endBeforeDeadline(res, req.headers)` once the stream has started. It
 *    ends the stream with a `retry:` hint and the browser's `EventSource` reconnects by itself,
 *    sending `Last-Event-ID` when the events carry an `id:`.
 *  - A WebSocket: call `closeBeforeDeadline(socket, req.headers)` when it opens. It closes the
 *    socket with `RESTART_CODE`; the browser client (`browserClient()`) reconnects on that close
 *    and on an unclean drop.
 *
 * Both return a function that cancels, and cancel themselves when the stream closes first.
 * Without the header, as on a laptop, nothing is cut. A stream that opens with less than the
 * margin left is ended after half the time left (`secondsToWait`), never at once. Whatever must
 * outlive one connection belongs in Postgres or in the browser, never in the connection. Same names, defaults and
 * vectors as the Python helper `ssc_app.reconnect`.
 */

import { readFileSync } from 'node:fs';

export const DEADLINE_HEADER = 'X-SSC-Request-Deadline';
export const RESTART_CODE = 1012;
export const DEFAULT_MARGIN_SECONDS = 30;
export const DEFAULT_RETRY_MS = 1000;

function header(headers) {
  if (!headers) return null;
  if (typeof headers.get === 'function') return headers.get(DEADLINE_HEADER) ?? null;
  const wanted = DEADLINE_HEADER.toLowerCase();
  for (const [name, value] of Object.entries(headers)) {
    if (name.toLowerCase() === wanted) return Array.isArray(value) ? value[0] : value;
  }
  return null;
}

/**
 * Seconds until the gateway's limit ends this request, never below 0; null when the request
 * carries no readable `X-SSC-Request-Deadline`. `now` (Unix seconds) is for tests.
 */
export function secondsLeft(headers, { now } = {}) {
  const raw = header(headers);
  if (typeof raw !== 'string' || !/^\s*[0-9]+\s*$/.test(raw)) return null;
  return Math.max(0, Number(raw) - (now ?? Date.now() / 1000));
}

/**
 * How long to keep a stream with `left` seconds to its deadline: until `margin` seconds before it,
 * or half of `left` when that is later, so a stream that opens inside the margin is not ended at
 * once and reopened in a loop. null, cut nothing, without a deadline or once it has passed (this
 * clock is ahead of the gateway's); the platform's limit ends that stream.
 */
export function secondsToWait(left, margin = DEFAULT_MARGIN_SECONDS) {
  if (left === null || left === undefined || left <= 0) return null;
  return Math.max(left - margin, left / 2);
}

function before(stream, headers, margin, action) {
  const wait = secondsToWait(secondsLeft(headers), margin);
  if (wait === null) return () => {};
  const timer = setTimeout(action, wait * 1000);
  const cancel = () => clearTimeout(timer);
  if (typeof stream.once === 'function') stream.once('close', cancel);
  else if (typeof stream.addEventListener === 'function') stream.addEventListener('close', cancel, { once: true });
  return cancel;
}

/**
 * End the event stream `res` (a Node `ServerResponse` or anything with `write` and `end`)
 * `secondsToWait` from now, writing `retry: <retryMs>` first so `EventSource`
 * reconnects that many milliseconds later.
 */
export function endBeforeDeadline(res, headers, { margin = DEFAULT_MARGIN_SECONDS, retryMs = DEFAULT_RETRY_MS } = {}) {
  return before(res, headers, margin, () => {
    res.write(`retry: ${retryMs}\n\n`);
    res.end();
  });
}

/**
 * Close `socket` (a `ws` WebSocket or anything with `close(code, reason)`) with `RESTART_CODE`
 * `secondsToWait` from now.
 */
export function closeBeforeDeadline(socket, headers, { margin = DEFAULT_MARGIN_SECONDS } = {}) {
  return before(socket, headers, margin, () => socket.close(RESTART_CODE, 'restart'));
}

/**
 * The browser side of `closeBeforeDeadline`, dependency-free JavaScript to serve or inline in a
 * page. It defines `sscSocket(url, options)`, a WebSocket that reconnects.
 */
export function browserClient() {
  return readFileSync(new URL('./browser.js', import.meta.url), 'utf8');
}
