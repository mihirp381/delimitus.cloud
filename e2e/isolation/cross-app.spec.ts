import { expect, test } from '@playwright/test';

import { A, answerTo, B, canLogin, fast, mark, NO_LOGIN, reached, sentToLogin, target, url, useSession, whoami } from './support';

/** A page on another site altogether, served by the test itself (`page.route`), never fetched. */
const ELSEWHERE = 'https://elsewhere.example';

/** Open a WebSocket to `address` from the page: whether it opened, its first message, the echo of
 * `ping`, and its close code (-1: still not closed after 10 seconds). */
function openSocket(address: string) {
  return new Promise<{ opened: boolean; first: string | null; echo: string | null; code: number }>((resolve) => {
    const ws = new WebSocket(address);
    const got = { opened: false, first: null as string | null, echo: null as string | null, code: -1 };
    ws.onopen = () => {
      got.opened = true;
    };
    ws.onmessage = (event) => {
      if (got.first === null) {
        got.first = String(event.data);
        ws.send('ping');
      } else {
        got.echo = String(event.data);
        ws.close(1000);
      }
    };
    ws.onclose = (event) => resolve({ ...got, code: event.code });
    setTimeout(() => resolve(got), 10_000);
  });
}

test.describe('one app cannot act on another in the same cell', () => {
  test.skip(!canLogin, NO_LOGIN);

  test.beforeEach(async ({ context }) => {
    await useSession(context, A, target.user);
    await useSession(context, B, target.user);
  });

  test('a POST from app A to app B is refused before it reaches B', async ({ page }) => {
    const m = mark();
    await page.goto(url(B, '/'));
    const own = await page.evaluate(async (m) => (await fetch(`/note?m=${m}-own`, { method: 'POST', body: 'x' })).status, m);
    expect(own, "B's own page may POST to B").toBe(200);

    await page.goto(url(A, '/'));
    const read = await page.evaluate(async ([b, m]) => {
      await fetch(`${b}/note?m=${m}-nocors`, { method: 'POST', mode: 'no-cors', credentials: 'include', body: 'x' }).catch(() => null);
      return fetch(`${b}/note?m=${m}-cors`, { method: 'POST', credentials: 'include', body: 'x' }).then(
        (r) => r.status,
        () => 'unreadable',
      );
    }, [url(B, ''), m] as const);
    expect(read).toBe('unreadable');
    const form = await answerTo(page, url(B, `/note?m=${m}-form`), () =>
      page.evaluate(([b, m]) => {
        const f = document.createElement('form');
        f.method = 'POST';
        f.action = `${b}/note?m=${m}-form`;
        document.body.append(f);
        f.submit();
      }, [url(B, ''), m] as const),
    );
    expect(form.status(), 'a form POST from A to B').toBe(403);
    await page.waitForURL(url(B, `/note?m=${m}-form`));
    expect(await reached(page, B, m)).toEqual([`post:${m}-own`]);
  });

  test('a WebSocket from app A to app B is refused; from B itself it opens', async ({ page }) => {
    const m = mark();
    await page.goto(url(B, '/'));
    const own = await page.evaluate(openSocket, `wss://${B}/echo?m=${m}-own`);
    expect(own.opened, "B's own page opens a WebSocket to B").toBe(true);
    expect(own.echo).toBe('ping');
    expect(JSON.parse(own.first ?? '{}').cookie ?? '', 'the session cookie never reaches the app').not.toMatch(/ssc/i);

    await page.goto(url(A, '/'));
    const cross = await page.evaluate(openSocket, `wss://${B}/echo?m=${m}-cross`);
    expect(cross.opened, "A's page opens a WebSocket to B").toBe(false);
    expect(await reached(page, B, m)).toEqual([`upgrade:${m}-own`]);
  });

  test('a script, an image or a frame on app A cannot load from app B', async ({ page }) => {
    const m = mark();
    const load = (tag: 'script' | 'img' | 'iframe', src: string) =>
      page.evaluate(
        ([tag, src]) =>
          new Promise<void>((done) => {
            const element = document.createElement(tag);
            element.addEventListener('load', () => done());
            element.addEventListener('error', () => done());
            setTimeout(done, 5000);
            element.setAttribute('src', src);
            document.body.append(element);
          }),
        [tag, src] as const,
      );
    await page.goto(url(B, '/'));
    await load('img', `/whoami?m=${m}-own`);
    await page.goto(url(A, '/'));
    for (const tag of ['script', 'img'] as const) await load(tag, url(B, `/whoami?m=${m}-${tag}`));
    const frame = await answerTo(page, url(B, `/?m=${m}-iframe`), () => load('iframe', url(B, `/?m=${m}-iframe`)));
    expect(frame.status(), 'a frame on A showing B').toBe(403);
    expect(await reached(page, B, m), 'only B loading from itself reached B').toEqual([`get:${m}-own`]);
  });

  test('a link from app A opens app B: a top-level GET navigation is allowed', async ({ page }) => {
    await page.goto(url(A, '/'));
    await page.evaluate((b) => {
      const a = document.createElement('a');
      a.href = b;
      a.id = 'to-b';
      a.textContent = 'to B';
      document.body.append(a);
    }, url(B, '/'));
    const answer = await answerTo(page, url(B, '/'), () => page.click('#to-b'));
    expect(answer.status()).toBe(200);
    await expect(page.locator('#app')).toHaveText('bravo');
    await expect(page.locator('#who')).toHaveText(target.user);
  });

  test('from another site a form POST to app B is refused and a link to it opens', async ({ page }) => {
    const m = mark();
    await page.route(`${ELSEWHERE}/**`, (route) => route.fulfill({ status: 200, contentType: 'text/html', body: '<h1>elsewhere</h1>' }));
    await page.goto(`${ELSEWHERE}/`);
    const form = await answerTo(page, url(B, `/note?m=${m}-site`), () =>
      page.evaluate((action) => {
        const f = document.createElement('form');
        f.method = 'POST';
        f.action = action;
        document.body.append(f);
        f.submit();
      }, url(B, `/note?m=${m}-site`)),
    );
    expect(form.status(), 'a cross-site form POST').toBe(403);
    await page.waitForURL(url(B, `/note?m=${m}-site`));

    await page.goto(`${ELSEWHERE}/`);
    await page.evaluate((b) => {
      const a = document.createElement('a');
      a.href = b;
      a.id = 'to-b';
      a.textContent = 'to B';
      document.body.append(a);
    }, url(B, '/'));
    const link = await answerTo(page, url(B, '/'), () => page.click('#to-b'));
    expect(link.status(), 'a cross-site link').toBe(200);
    await expect(page.locator('#app')).toHaveText('bravo');
    expect(await reached(page, B, m)).toEqual([]);
  });

  test('/.ssc/logout from app A does not sign the person out of B', async ({ page }) => {
    await page.goto(url(A, '/'));
    await page.evaluate(async (b) => {
      await fetch(`${b}/.ssc/logout`, { mode: 'no-cors', credentials: 'include' }).catch(() => null);
      await new Promise((done) => {
        const img = new Image();
        img.onload = img.onerror = done;
        img.src = `${b}/.ssc/logout`;
      });
      const a = document.createElement('a');
      a.href = `${b}/.ssc/logout`;
      a.id = 'logout-b';
      a.textContent = 'log out of B';
      document.body.append(a);
    }, url(B, ''));
    const fromA = await answerTo(page, url(B, '/.ssc/logout'), () => page.click('#logout-b'));
    expect(fromA.status(), 'a link from A to B /.ssc/logout').toBe(404);
    expect((await whoami(page, B)).sub, 'still signed in to B').toBe(target.user);

    // B's own logout ends the person's browser session at the auth host. Live, every case shares
    // the one session the night signed in, so only the rig signs out for real.
    if (!fast) return;
    await page.evaluate(() => {
      const a = document.createElement('a');
      a.href = '/.ssc/logout';
      a.id = 'logout';
      a.textContent = 'log out';
      document.body.append(a);
    });
    await page.click('#logout');
    await page.waitForURL((u) => u.host !== B);
    await sentToLogin(page, url(B, '/'));
  });
});
