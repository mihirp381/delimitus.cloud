import { expect, type Response, test } from '@playwright/test';

import {
  A,
  B,
  canLogin,
  canSeal,
  holdCode,
  host,
  NO_LOGIN,
  NO_SESSIONS,
  otherBrowser,
  putSession,
  SESSION,
  sentToLogin,
  setCookies,
  signIn,
  startLogin,
  target,
  url,
  useSession,
} from './support';

async function headersOf(response: Response): Promise<string[]> {
  const drop = new Set(['date', 'x-envoy-upstream-service-time', 'x-cloud-trace-context', 'traceparent']);
  return (await response.headersArray()).filter((h) => !drop.has(h.name.toLowerCase())).map((h) => `${h.name.toLowerCase()}: ${h.value}`).sort();
}

/** The gateway's `Set-Cookie` headers on the way in (the login cookie) and at the hand-back (the
 * session cookie: no `Domain`, `Path=/`, `Secure`, `HttpOnly`, `SameSite=Lax`). Only Chromium reports
 * `Set-Cookie` on a redirect; on WebKit the stored cookie's attributes are what the case checks. */
async function expectHeadersAsSent(answers: Response[]): Promise<void> {
  const first = answers.find((r) => r.url() === url(A, '/') && r.status() === 302);
  expect((await setCookies(first!)).some((v) => v.startsWith('__Host-ssc-login='))).toBe(true);
  const handBack = answers.find((r) => r.url().startsWith(url(A, '/.ssc/callback?')));
  expect(handBack?.status()).toBe(302);
  const set = (await setCookies(handBack!)).find((v) => v.startsWith(`${SESSION}=`)) ?? '';
  const attributes = set.split(';').slice(1).map((a) => a.trim().toLowerCase());
  expect(attributes).toEqual(expect.arrayContaining(['path=/', 'secure', 'httponly', 'samesite=lax']));
  expect(attributes.filter((a) => a.startsWith('domain'))).toEqual([]);
}

test.describe('login sessions', () => {
  test('login round trip: the callback sets a host-only __Host- cookie that does not open app B', async ({ page, context, browserName }) => {
    test.skip(!canLogin, NO_LOGIN);
    const answers: Response[] = [];
    const logins: string[] = [];
    page.on('response', (r) => answers.push(r));
    page.on('request', (r) => {
      if (r.url().startsWith(`${target.authUrl}/login?`)) logins.push(r.url());
    });
    await signIn(page, A);
    const login = new URL(logins[0] ?? 'x:/');
    expect(login.searchParams.get('return_to')).toBe(url(A, '/'));
    expect(login.searchParams.get('binding')).toMatch(/^[A-Za-z0-9_-]{43}$/);
    const cookies = await context.cookies(url(A, '/'));
    const session = cookies.find((c) => c.name === SESSION);
    expect(session).toMatchObject({ domain: A, path: '/', secure: true, httpOnly: true, sameSite: 'Lax' });
    expect(cookies.map((c) => c.name)).not.toContain('__Host-ssc-login');
    if (browserName === 'chromium') await expectHeadersAsSent(answers);

    await putSession(context, B, session!.value);
    await sentToLogin(page, url(B, '/'));
  });

  test('a login code that was used does not sign anyone in again', async ({ page, browser }) => {
    test.skip(!canLogin, NO_LOGIN);
    const callback = await signIn(page, A);
    const again = await page.goto(callback);
    expect(again?.status(), 'the same browser, again').toBe(400);

    const other = await startLogin(await otherBrowser(browser), A);
    const stolen = await other.goto(callback);
    expect(stolen?.status(), 'another browser with its own login nonce').toBe(400);
    await sentToLogin(other, url(A, '/'));
  });

  test('a login code taken before its owner uses it signs in nobody', async ({ page, browser }) => {
    test.skip(!canLogin, NO_LOGIN);
    const held = await holdCode(page, A);
    const other = await startLogin(await otherBrowser(browser), A);
    expect((await other.goto(held))?.status(), 'the thief, with its own nonce').toBe(400);
    await sentToLogin(other, url(A, '/'));
    expect((await page.goto(held))?.status(), 'the owner, after the code was tried').toBe(400);
  });

  test('a person not granted app A gets exactly what an address with no app gets', async ({ page, context }) => {
    test.skip(!canSeal || target.outsider === '', NO_SESSIONS);
    const nowhere = host(`nothere${Date.now().toString(36)}`);
    await useSession(context, A, target.outsider);
    await useSession(context, nowhere, target.outsider);
    const forbidden = await page.goto(url(A, '/'));
    const absent = await page.goto(url(nowhere, '/'));
    expect(forbidden?.status()).toBe(404);
    expect(absent?.status()).toBe(forbidden?.status());
    expect((await absent!.body()).equals(await forbidden!.body())).toBe(true);
    expect(await headersOf(absent!)).toEqual(await headersOf(forbidden!));
  });
});
