import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { setTimeout as sleep } from 'node:timers/promises';

import '../browser.js';
import {
  DEADLINE_HEADER,
  DEFAULT_MARGIN_SECONDS,
  DEFAULT_RETRY_MS,
  RESTART_CODE,
  browserClient,
  closeBeforeDeadline,
  endBeforeDeadline,
  secondsLeft,
  secondsToWait,
} from '../index.js';
import { startApp } from './app.js';

const VECTORS = JSON.parse(readFileSync(new URL('../../../../conformance/reconnect/vectors.json', import.meta.url), 'utf8'));

async function until(check, ms = 8000) {
  const stop = Date.now() + ms;
  while (!check()) {
    if (Date.now() > stop) throw new Error('timed out');
    await sleep(20);
  }
}

function continuous(values) {
  return values.every((v, i) => v === i);
}

for (const c of VECTORS.cases) {
  test(`vector: ${c.name}`, () => {
    assert.equal(secondsLeft(c.headers, { now: VECTORS.now }), c.expect);
  });
}

for (const c of VECTORS.waits) {
  test(`wait vector: ${c.name}`, () => {
    assert.equal(secondsToWait(c.left, c.margin), c.expect);
  });
}

test('names and defaults match the Python helper', () => {
  assert.deepEqual(
    { deadline_header: DEADLINE_HEADER, restart_code: RESTART_CODE, margin_seconds: DEFAULT_MARGIN_SECONDS, retry_ms: DEFAULT_RETRY_MS },
    VECTORS.constants,
  );
});

test('a Headers object and repeated headers are read too', () => {
  assert.equal(secondsLeft(new Headers({ [DEADLINE_HEADER]: '1790000005' }), { now: VECTORS.now }), 5);
  assert.equal(secondsLeft({ 'x-ssc-request-deadline': ['1790000007', '1'] }, { now: VECTORS.now }), 7);
  assert.equal(secondsLeft(undefined), null);
});

test('without a deadline or past it nothing is ended; inside the margin it ends after half the time left', async () => {
  const written = [];
  const at = [];
  const res = { write: (s) => written.push(s), end: () => written.push('end') };
  const socket = { close: (...args) => at.push(Date.now()) && written.push(args) };
  const passed = { [DEADLINE_HEADER]: String(Math.floor(Date.now() / 1000) - 5) };
  endBeforeDeadline(res, {});
  closeBeforeDeadline(socket, {});
  endBeforeDeadline(res, passed, { margin: 0 });
  closeBeforeDeadline(socket, passed);
  await sleep(50);
  assert.deepEqual(written, []);
  const soon = { [DEADLINE_HEADER]: String(Math.floor(Date.now() / 1000) + 2) };
  const left = secondsLeft(soon);
  const start = Date.now();
  endBeforeDeadline(res, soon, { margin: 10, retryMs: 250 });
  closeBeforeDeadline(socket, soon, { margin: 10 });
  const cancel = closeBeforeDeadline(socket, soon, { margin: 10 });
  cancel();
  await until(() => written.length === 3, 4000);
  const took = (at[0] - start) / 1000;
  assert.deepEqual(written, ['retry: 250\n\n', 'end', [RESTART_CODE, 'restart']]);
  assert.ok(took >= left / 2 - 0.05 && took < left, `ended after ${took} s of ${left}, not at once`);
});

test('an EventSource keeps its stream across the limit without the user acting', async () => {
  const app = await startApp({ limit: 2, margin: 0.5, retryMs: 50 });
  const source = new EventSource(`${app.url}/events`);
  const got = [];
  source.onmessage = (event) => got.push({ n: Number(event.data), id: event.lastEventId, at: Date.now() });
  try {
    await until(() => app.connections.length >= 3 && got.at(-1)?.at > app.connections[1].deadline * 1000);
  } finally {
    source.close();
    await app.close();
  }
  assert.ok(continuous(got.map((e) => e.n)), 'no event lost or repeated');
  assert.ok(got.every((e) => e.id === String(e.n)));
  assert.ok(got.at(-1).at > app.connections[0].deadline * 1000, 'events kept coming past the first limit');
  const [first, ...later] = app.connections;
  assert.equal(first.resume, null);
  for (const c of later) assert.match(c.resume, /^[0-9]+$/, 'the browser resumed with Last-Event-ID');
  for (const c of app.connections.filter((c) => c.ended !== null)) {
    assert.ok(c.ended < c.deadline * 1000, 'the app ended each stream before its limit');
  }
});

test('the browser client keeps a WebSocket across the limit without the user acting', async () => {
  const app = await startApp({ limit: 2, margin: 0.5 });
  let last = -1;
  const got = [];
  const opened = [];
  let gaveUp = null;
  const socket = globalThis.sscSocket(() => `${app.wsUrl}?after=${last}`, {
    maxDelayMs: 200,
    onopen: () => opened.push(Date.now()),
    onmessage: (event) => {
      last = Number(event.data);
      got.push({ n: last, at: Date.now() });
    },
    onclose: (event) => {
      gaveUp = event;
    },
  });
  try {
    await until(() => app.connections.length >= 3 && got.at(-1)?.at > app.connections[1].deadline * 1000);
    assert.equal(socket.send('hello'), true);
  } finally {
    socket.close();
    await app.close();
  }
  assert.ok(continuous(got.map((e) => e.n)), 'no message lost or repeated');
  assert.ok(got.at(-1).at > app.connections[0].deadline * 1000, 'messages kept coming past the first limit');
  assert.ok(opened.length >= 3);
  assert.equal(gaveUp, null);
  const ended = app.connections.slice(0, -1);
  for (const c of ended) {
    assert.equal(c.closeCode, RESTART_CODE, 'the app closed with the restart code');
    assert.ok(c.ended < c.deadline * 1000, 'the app closed each socket before its limit');
  }
});

test('the browser client comes back after an unclean drop', async () => {
  const app = await startApp({ limit: null, ws: 'drop' });
  let last = -1;
  const got = [];
  const socket = globalThis.sscSocket(() => `${app.wsUrl}?after=${last}`, {
    delayMs: 20,
    onmessage: (event) => {
      last = Number(event.data);
      got.push(last);
    },
  });
  try {
    await until(() => app.connections.length === 2 && got.length >= 6);
  } finally {
    socket.close();
    await app.close();
  }
  assert.equal(app.connections[0].closeCode, 1006);
  assert.equal(app.connections[1].resume, String(got[got.indexOf(Number(app.connections[1].resume))]));
  assert.ok(continuous(got), 'it resumed where the dropped socket stopped');
});

test('the browser client stops on a clean close and gives up when it cannot get back in', async () => {
  for (const [ws, connections, code] of [
    ['done', 1, 1000],
    ['refuse', 3, 1006],
  ]) {
    const app = await startApp({ limit: null, ws });
    let gaveUp = null;
    globalThis.sscSocket(app.wsUrl, { delayMs: 10, attempts: 2, onclose: (event) => (gaveUp = event) });
    try {
      await until(() => gaveUp !== null);
      await sleep(100);
    } finally {
      await app.close();
    }
    assert.equal(gaveUp.code, code, ws);
    assert.equal(app.connections.length, connections, ws);
  }
});

test('browserClient() is the shipped browser file', () => {
  assert.equal(browserClient(), readFileSync(new URL('../browser.js', import.meta.url), 'utf8'));
});
