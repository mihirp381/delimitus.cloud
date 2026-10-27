import { expect, type Page, test } from '@playwright/test';

const AUTH = 'https://auth.delimitus.com';
const ORG = 'org_aaaaaaaaaaaaaaaaaaaa';
const INVITE = 'inv_0123456789abcdef';

async function continueTo(page: Page): Promise<URL> {
  const sent = page.waitForRequest(`${AUTH}/authorize?**`);
  await page.getByRole('button', { name: 'Continue' }).click();
  return new URL((await sent).url());
}

async function postTo(page: Page): Promise<{ url: string; form: URLSearchParams }> {
  const sent = page.waitForRequest((r) => r.url().startsWith(`${AUTH}/authorize`) && r.method() === 'POST');
  await page.getByRole('button', { name: 'Continue' }).click();
  const request = await sent;
  return { url: request.url(), form: new URLSearchParams(request.postData() ?? '') };
}

test('the sign-in field takes an organisation id or an invite code, remembers it, and forgets a miss', async ({
  page,
}) => {
  await page.route(`${AUTH}/**`, (route) => route.fulfill({ status: 200, contentType: 'text/plain', body: 'auth' }));
  const field = page.getByLabel('Organisation id or invite code');

  await page.goto('/login');
  await field.fill('acme');
  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page.getByRole('alert')).toHaveText('That is not an organisation id or invite code.');

  await field.fill(` ${ORG.toUpperCase()} `);
  const byOrg = await continueTo(page);
  expect(byOrg.searchParams.get('org')).toBe(ORG);
  expect(byOrg.searchParams.has('invite')).toBe(false);

  await page.goto('/login');
  await expect(field).toHaveValue(ORG);
  await field.fill(INVITE);
  const byInvite = await postTo(page);
  expect(byInvite.url).toBe(`${AUTH}/authorize`);
  expect(byInvite.form.get('invite')).toBe(INVITE);
  expect(byInvite.form.has('org')).toBe(false);
  expect(byInvite.form.get('client_id')).toBe('ssc-console');
  expect(byInvite.form.get('code_challenge_method')).toBe('S256');

  const back = new URLSearchParams({
    error: 'access_denied',
    error_description: 'no_sign_in',
    state: byInvite.form.get('state') ?? '',
    iss: AUTH,
  });
  await page.goto(`/auth/callback?${back.toString()}`);
  await expect(page).toHaveURL(/\/login\?error=no_sign_in/);
  await expect(page.getByRole('alert')).toHaveText(
    'We could not find a sign-in for that organisation or code. Check it and try again.',
  );
  await expect(field).toHaveValue('');
});
