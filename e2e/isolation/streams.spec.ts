import { expect, test } from '@playwright/test';

import { B, canSeal, fast, LIMITED, NO_SESSIONS, target, url, useSession } from './support';

type Cut = { opened: number; closed: number; deadline: number | null };
type Counted = { opens: number; seen: number[] };

declare global {
  /** What the app's `/reconnect` page keeps: opens and counts over `sscSocket` and `EventSource`. */
  interface Window {
    iso: { ws: Counted; es: Counted };
  }
}

/** From the page, open a WebSocket and an event stream that the app never ends, and report when
 * each opened, when it was cut, and the deadline the app was told (Unix seconds). */
function rawStreams() {
  const now = () => Date.now() / 1000;
  const deadlineOf = (data: string) => Number(JSON.parse(data).deadline);
  const ws = new Promise<Cut>((resolve) => {
    const got: Cut = { opened: 0, closed: 0, deadline: null };
    const socket = new WebSocket(`wss://${location.host}/raw-ws`);
    socket.onopen = () => {
      got.opened = now();
    };
    socket.onmessage = (event) => {
      if (got.deadline === null) got.deadline = deadlineOf(String(event.data));
    };
    socket.onclose = () => {
      got.closed = now();
      resolve(got);
    };
  });
  const es = new Promise<Cut>((resolve) => {
    const got: Cut = { opened: 0, closed: 0, deadline: null };
    const events = new EventSource('/raw-events');
    events.onopen = () => {
      got.opened = now();
    };
    events.onmessage = (event) => {
      if (got.deadline === null) got.deadline = deadlineOf(String(event.data));
    };
    events.onerror = () => {
      if (got.opened === 0) return;
      got.closed = now();
      events.close();
      resolve(got);
    };
  });
  return Promise.all([ws, es]).then(([ws, es]) => ({ ws, es }));
}

test.describe('streams', () => {
  test.skip(!canSeal, NO_SESSIONS);

  test('server-sent events arrive as the app sends them, not buffered', async ({ page, context }) => {
    await useSession(context, B, target.user);
    await page.goto(url(B, '/'));
    const times = await page.evaluate(
      () =>
        new Promise<number[]>((resolve, reject) => {
          const started = performance.now();
          const got: number[] = [];
          const events = new EventSource('/ticks');
          events.onmessage = () => {
            got.push(performance.now() - started);
            if (got.length === 3) {
              events.close();
              resolve(got);
            }
          };
          setTimeout(() => {
            events.close();
            reject(new Error(`${got.length} events in 15 seconds`));
          }, 15_000);
        }),
    );
    expect(times[1]! - times[0]!).toBeGreaterThan(700);
    expect(times[2]! - times[0]!).toBeGreaterThan(1500);
  });

  test('at the limit a WebSocket and an event stream are cut, the deadline was announced, and a page with the helpers reconnects by itself', async ({ page, context }) => {
    test.skip(!fast && !target.nightly, 'The limit is 60 minutes in a staging cell, so this case runs only in the nightly (SSC_ISO_NIGHTLY=1).');
    const limit = target.limitSeconds;
    const slack = Math.max(4, limit * 0.02);
    test.setTimeout((limit * 2 + 120) * 1000);
    await useSession(context, LIMITED, target.user);
    const helpers = await context.newPage();
    await page.goto(url(LIMITED, '/'));
    const raw = page.evaluate(rawStreams);
    await helpers.goto(url(LIMITED, '/reconnect'));

    const { ws, es } = await raw;
    for (const [name, cut] of [['WebSocket', ws], ['event stream', es]] as const) {
      expect(cut.deadline, `${name}: the app was told the deadline`).not.toBeNull();
      expect(Math.abs(cut.deadline! - cut.opened - limit), `${name}: deadline ${cut.deadline} for a stream opened at ${cut.opened}`).toBeLessThanOrEqual(3);
      expect(cut.closed - cut.opened, `${name}: cut after`).toBeGreaterThan(limit - 3);
      expect(cut.closed - cut.opened, `${name}: cut after`).toBeLessThan(limit + slack);
    }

    const opens = fast ? 3 : 2;
    await expect
      .poll(() => helpers.evaluate(() => Math.min(window.iso.ws.opens, window.iso.es.opens)), { timeout: (limit * 2 + 60) * 1000, intervals: [1000] })
      .toBeGreaterThanOrEqual(opens);
    const seen = await helpers.evaluate(() => ({ ws: window.iso.ws.seen, es: window.iso.es.seen }));
    for (const [name, counts] of Object.entries(seen)) {
      expect(counts.length, `${name}: counted across reconnects`).toBeGreaterThan(opens);
      expect(counts, `${name}: no count lost or repeated across reconnects`).toEqual(counts.map((_, i) => counts[0]! + i));
    }
  });
});
