import { type BrowserContext, expect, type Page, test } from '@playwright/test';

import {
  A,
  B,
  canLogin,
  fast,
  NEEDS_SECOND_USER,
  NO_LOGIN,
  seal,
  SESSION,
  sentToLogin,
  setCookies,
  signIn,
  target,
  url,
  useSession,
  WAKE,
  whoami,
} from './support';

/** From app A's page, try every way of planting `value` as a session cookie on the cell's other
 * hosts: `document.cookie` for the cell's domain and its parent, then a `Set-Cookie` from A. */
async function toss(page: Page, value: string): Promise<void> {
  await page.goto(url(A, '/'));
  await page.evaluate(
    ([value, base]) => {
      const parent = base.split('.').slice(1).join('.');
      for (const name of ['__Host-ssc-session', '__host-ssc-session', '__Secure-ssc-session']) {
        for (const domain of [base, parent]) {
          document.cookie = `${name}=${value}; Domain=${domain}; Path=/; Secure; SameSite=Lax`;
        }
      }
    },
    [value, target.base] as const,
  );
  await page.goto(url(A, `/toss?value=${encodeURIComponent(value)}`));
}

async function sessionOn(context: BrowserContext, name: string): Promise<string[]> {
  return (await context.cookies(url(name, '/'))).filter((c) => c.name === SESSION).map((c) => `${c.domain} ${c.value}`);
}

test.describe('cookies', () => {
  test('cookie tossing from app A cannot replace the session on app B', async ({ page, context }) => {
    test.skip(!fast, NEEDS_SECOND_USER);
    const theirs = await seal(B, target.otherUser);
    await useSession(context, A, target.user);
    await useSession(context, B, target.user);
    const before = await sessionOn(context, B);
    await toss(page, theirs);
    expect(await sessionOn(context, B), 'the only session cookie B gets is its own').toEqual(before);
    const seen = await whoami(page, B);
    expect(seen.sub, 'B still sees the person, not the one whose session A tossed').toBe(target.user);
    expect(seen.cookie ?? '', 'no platform cookie reaches B').not.toMatch(/ssc/i);
  });

  test('cookie tossing from app A cannot sign a person in to app B as someone else', async ({ page, context }) => {
    test.skip(!fast, NEEDS_SECOND_USER);
    const theirs = await seal(B, target.otherUser);
    await useSession(context, A, target.user);
    await toss(page, theirs);
    expect(await sessionOn(context, B)).toEqual([]);
    await sentToLogin(page, url(B, '/'));
  });

  test('document.cookie cannot see the __Host- session cookie', async ({ page, context }) => {
    test.skip(!canLogin, NO_LOGIN);
    await signIn(page, B);
    expect((await context.cookies(url(B, '/'))).map((c) => c.name)).toContain(SESSION);
    expect(await page.evaluate(() => document.cookie)).not.toContain('ssc');
    await signIn(page, A);
    expect(await page.evaluate(() => document.cookie)).not.toContain('ssc');
  });

  test("an app's platform-named Set-Cookie never reaches the browser, and the session cookie never reaches the app", async ({ page, context }) => {
    test.skip(!canLogin, NO_LOGIN);
    await useSession(context, B, target.user);
    const before = await sessionOn(context, B);
    const answer = await page.goto(url(B, '/set-cookies'));
    expect(answer?.status()).toBe(200);
    const set = await setCookies(answer!);
    expect(set.filter((v) => /^\s*__(host|secure)-ssc-(session|x|y)=/i.test(v)), "the app's platform-named cookies").toEqual([]);
    expect(set.some((v) => v.startsWith('iso-app=1')), "the app's own cookie still arrives").toBe(true);
    expect(await sessionOn(context, B)).toEqual(before);
    const names = (await context.cookies(url(B, '/'))).map((c) => c.name);
    expect(names.filter((n) => /^__(host|secure)-ssc/i.test(n) && n !== WAKE)).toEqual([SESSION]);
    const seen = await whoami(page, B);
    expect(seen.cookie ?? '').toContain('iso-app=1');
    expect(seen.cookie ?? '').not.toMatch(/ssc/i);
  });
});
