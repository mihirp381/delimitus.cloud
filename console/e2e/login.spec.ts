import { expect, type Page, test } from '@playwright/test';

const AUTH = 'https://auth.delimitus.com';
const ORG = 'org_aaaaaaaaaaaaaaaaaaaa';
const INVITE = 'inv_0123456789abcdef';

async function continueTo(page: Page): Promise<URL> {
  const sent = page.waitForRequest(`${AUTH}/authorize?**`);
  await page.getByRole('button', { name: 'Continue' }).click();
  return new URL((await sent).url());
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
  const byInvite = await continueTo(page);
  expect(byInvite.searchParams.get('invite')).toBe(INVITE);
  expect(byInvite.searchParams.has('org')).toBe(false);

  const back = new URLSearchParams({
    error: 'access_denied',
    error_description: 'no_sign_in',
    state: byInvite.searchParams.get('state') ?? '',
    iss: AUTH,
  });
  await page.goto(`/auth/callback?${back.toString()}`);
  await expect(page).toHaveURL(/\/login\?error=no_sign_in/);
  await expect(page.getByRole('alert')).toHaveText(
    'We could not find a sign-in for that organisation or code. Check it and try again.',
  );
  await expect(field).toHaveValue('');
});
