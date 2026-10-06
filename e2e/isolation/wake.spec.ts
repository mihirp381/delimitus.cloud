import { expect, test } from '@playwright/test';

import { A, canLogin, mark, NO_LOGIN, target, url, useSession, WAKE } from './support';

test.describe('a sleeping app wakes', () => {
  test.skip(!canLogin, NO_LOGIN);

  test.beforeEach(async ({ context }) => {
    await useSession(context, A, target.user);
  });

  test('a page load gets the waking page, then the app by itself', async ({ page }) => {
    const address = url(A, `/cold/${mark()}`);
    const first = await page.goto(address);
    expect(first?.status()).toBe(503);
    expect(await first!.text()).toContain('Waking up');
    await expect(page.locator('#awake')).toHaveText('awake', { timeout: 20_000 });
    expect(page.url()).toBe(address);
  });

  test('a script call waits for the app instead of getting the waking page', async ({ page, context }) => {
    await page.goto(url(A, '/'));
    await context.clearCookies({ name: WAKE });
    const got = await page.evaluate(async (path) => {
      const started = performance.now();
      const response = await fetch(path);
      return { status: response.status, body: await response.text(), ms: performance.now() - started };
    }, `/cold/${mark()}`);
    expect(got.status).toBe(200);
    expect(got.body).toContain('awake');
    expect(got.body).not.toContain('Waking up');
    expect(got.ms).toBeGreaterThan(2500);
  });

  test('a WebSocket upgrade waits for the app instead of getting the waking page', async ({ page, context }) => {
    await page.goto(url(A, '/'));
    await context.clearCookies({ name: WAKE });
    const got = await page.evaluate(
      () =>
        new Promise<{ first: string | null; ms: number }>((resolve) => {
          const started = performance.now();
          const ws = new WebSocket(`wss://${location.host}/cold-ws`);
          ws.onmessage = (event) => {
            ws.close(1000);
            resolve({ first: String(event.data), ms: performance.now() - started });
          };
          ws.onclose = () => resolve({ first: null, ms: performance.now() - started });
        }),
    );
    expect(got.first).toBe('awake');
    expect(got.ms).toBeGreaterThan(2500);
  });
});
