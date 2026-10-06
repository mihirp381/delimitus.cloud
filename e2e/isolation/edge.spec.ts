import { expect, type Page, test } from '@playwright/test';

import {
  A,
  B,
  canLogin,
  control,
  fast,
  holdCode,
  host,
  mark,
  NO_LOGIN,
  NO_PEER_CELL,
  otherBrowser,
  putSession,
  reached,
  SESSION,
  sentToLogin,
  target,
  url,
  useSession,
} from './support';

/** A POST from `name`'s own page (the gateway's answer is on that origin), and its status. */
async function postFromOwnPage(page: Page, name: string, m: string): Promise<number> {
  expect(new URL(page.url()).host).toBe(name);
  return page.evaluate(async (m) => (await fetch(`/note?m=${m}`, { method: 'POST', body: 'x' })).status, m);
}

test.describe('the public entry', () => {
  test("the gateway's run.app address, called from the internet, is refused", async ({ page }) => {
    test.skip(fast, 'Docker has no run.app address: in the rig the gateway is reachable only through the load balancer stand-in. Runs nightly against a cell.');
    test.skip(target.gatewayRunApp === null, 'SSC_ISO_GATEWAY_RUN_APP (the gateway service run.app host) is not set.');
    const origin = /^https:\/\//.test(target.gatewayRunApp!) ? target.gatewayRunApp! : `https://${target.gatewayRunApp}`;
    for (const path of ['/', '/.ssc/callback?code=x', '/whoami']) {
      const got = await page.goto(`${origin}${path}`).then(
        (r) => r?.status() ?? 0,
        () => 'unreachable',
      );
      expect([403, 404, 'unreachable'], `${origin}${path}`).toContain(got);
    }
  });
});

test.describe('fail closed', () => {
  test.skip(!fast, 'Needs the rig: a live cell is not switched off from a test.');

  test.afterEach(async () => {
    await control('authoriser', true);
    await control('snapshot', true);
  });

  test('with the authoriser down every request is 503 and nothing reaches an app', async ({ page, context }) => {
    const m = mark();
    const nowhere = host(`nothere${Date.now().toString(36)}`);
    await useSession(context, A, target.user);
    await useSession(context, nowhere, target.user);
    await control('authoriser', false);
    for (const address of [url(A, '/'), url(B, '/'), url(nowhere, '/'), url(A, '/.ssc/callback?code=x&next=/')]) {
      expect((await page.goto(address))?.status(), address).toBe(503);
    }
    await page.goto(url(A, '/'));
    expect(await postFromOwnPage(page, A, `${m}-down`)).toBe(503);
    await control('authoriser', true);
    await expect.poll(async () => (await page.goto(url(A, '/')))?.status(), { timeout: 15_000 }).toBe(200);
    expect(await reached(page, A, m)).toEqual([]);
  });

  test('with no current snapshot a signed-in request is 503, one without a session goes to sign in, and nothing reaches an app', async ({ page, context }) => {
    const m = mark();
    const nowhere = host(`nothere${Date.now().toString(36)}`);
    await useSession(context, A, target.user);
    await useSession(context, nowhere, target.user);
    await control('snapshot', false);
    expect((await page.goto(url(A, '/')))?.status()).toBe(503);
    expect(await postFromOwnPage(page, A, `${m}-stale`)).toBe(503);
    expect((await page.goto(url(nowhere, '/')))?.status()).toBe(503);
    await sentToLogin(page, url(B, '/'));
    await control('snapshot', true);
    expect((await page.goto(url(A, '/')))?.status()).toBe(200);
    expect(await reached(page, A, m)).toEqual([]);
  });
});

test.describe('two cells', () => {
  test.skip(fast, 'The rig is one cell; this needs two deployed cells, so it runs nightly.');
  test.skip(target.peer === null, NO_PEER_CELL);

  test('a cell-1 session cookie opens nothing in cell 2', async ({ page, context }) => {
    test.skip(!canLogin, NO_LOGIN);
    const there = host('alpha', target.peer!);
    await useSession(context, A, target.user);
    await useSession(context, B, target.user);
    for (const name of [A, B]) {
      const mine = (await context.cookies(url(name, '/'))).find((c) => c.name === SESSION);
      await putSession(context, there, mine!.value);
      await sentToLogin(page, url(there, '/'));
    }
  });

  test('a login code issued for a cell-1 host is refused by cell 2', async ({ page, browser }) => {
    test.skip(!canLogin, NO_LOGIN);
    const there = host('alpha', target.peer!);
    const held = new URL(await holdCode(page, A));
    const other = await otherBrowser(browser);
    await sentToLogin(other, url(there, '/'));
    const presented = url(there, `/.ssc/callback${held.search}`);
    expect((await other.goto(presented))?.status()).toBe(400);
  });
});
