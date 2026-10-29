import { expect, test } from '@playwright/test';

import { A, answerTo, fast, LIMITED, mark, reached, SESSION, sentToLogin, target, url, useSession } from './support';

/** Submit a form POST to `action` from the page. */
function post(action: string) {
  const f = document.createElement('form');
  f.method = 'POST';
  f.action = action;
  document.body.append(f);
  f.submit();
}

test.describe('form targets and ended sessions', () => {
  test.skip(!fast, "Needs the rig: its snapshot names A as a form target of B's preview.");

  test("a form POST to another app named in form-action is sent, and that app's gateway refuses it", async ({ page, context }) => {
    const m = mark();
    await useSession(context, LIMITED, target.user);
    await useSession(context, A, target.user);
    await page.goto(url(LIMITED, '/'));
    const sent = url(A, `/note?m=${m}-target`);
    const answer = await answerTo(page, sent, () => page.evaluate(post, sent));
    expect(answer.status(), 'a same-site form POST from another app').toBe(403);
    await page.waitForURL(sent);
    expect(await reached(page, A, m)).toEqual([]);
  });

  test('a form posted after the session ended gets a sign-in page that returns to the form', async ({ page, context }) => {
    const m = mark();
    await useSession(context, A, target.user);
    await page.goto(url(A, '/'));
    await context.clearCookies({ name: SESSION, domain: A });
    const sent = url(A, `/note?m=${m}-ended`);
    const answer = await answerTo(page, sent, () => page.evaluate(post, sent));
    expect(answer.status()).toBe(401);
    expect(answer.headers()['cache-control']).toBe('no-store');
    await expect(page.locator('h1')).toHaveText('Your session ended');
    await sentToLogin(page, url(A, '/'), () => page.getByRole('link', { name: 'Sign in again' }).click());
    await useSession(context, A, target.user);
    expect(await reached(page, A, m)).toEqual([]);
  });
});
