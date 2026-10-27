import { randomUUID } from 'node:crypto';
import { expect, type APIRequestContext, test } from '@playwright/test';

const token = process.env.SSC_E2E_TOKEN ?? '';
const operatorToken = process.env.SSC_E2E_OPERATOR_TOKEN ?? '';
const apiUrl = process.env.SSC_API_URL ?? '';

async function addPerson(request: APIRequestContext, name: string, stamp: string, status: string): Promise<void> {
  const response = await request.post(`${apiUrl}/internal/v1/directory/users`, {
    headers: { Authorization: `Bearer ${operatorToken}`, 'Idempotency-Key': randomUUID() },
    data: {
      issuer: 'https://dev.invalid',
      subject: `roster-${name}-${stamp}`,
      display_name: `${name} Roster ${stamp}`,
      email: `${name.toLowerCase()}-${stamp}@example.com`,
      role: 'member',
      status,
    },
  });
  expect(response.status(), await response.text()).toBe(200);
}

test('the roster: search, active or all, and a person opened', async ({ page, request }) => {
  expect(token, 'run through e2e/run.mjs, which mints the token').not.toBe('');
  const stamp = Date.now().toString(36);
  await addPerson(request, 'Rae', stamp, 'active');
  await addPerson(request, 'Gus', stamp, 'deactivated');

  await page.goto('/people');
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel('API token').fill(token);
  await page.getByRole('button', { name: 'Sign in' }).click();

  const roster = page.getByRole('table', { name: 'People, by name' });
  await expect(roster.getByRole('columnheader')).toHaveText(['Name', 'Email', 'Role', 'State', 'Last sign-in']);
  await expect(roster.getByRole('row').filter({ hasText: 'Dev Admin' }).getByRole('cell').nth(2)).toHaveText('admin');
  await expect(roster).not.toContainText('usr_');

  await page.getByLabel('Search people').fill(`roster ${stamp}`.toUpperCase());
  await expect(roster.getByRole('row')).toHaveCount(2);
  const rae = roster.getByRole('row').filter({ hasText: `Rae Roster ${stamp}` });
  await expect(rae.getByRole('cell')).toHaveText([
    `Rae Roster ${stamp}`,
    `rae-${stamp}@example.com`,
    'member',
    'active',
    'Never',
  ]);

  await page.getByRole('group', { name: 'Shown' }).getByText('All', { exact: true }).click();
  await expect(page.getByRole('radio', { name: 'All' })).toBeChecked();
  await expect(roster.getByRole('row')).toHaveCount(3);
  await expect(roster.getByRole('row').nth(1).getByRole('cell').nth(3)).toHaveText('deactivated');

  await rae.getByRole('button', { name: `Rae Roster ${stamp}` }).click();
  const dialog = page.getByRole('dialog', { name: `Rae Roster ${stamp}` });
  await expect(dialog).toContainText(`rae-${stamp}@example.com`);
  await expect(dialog.getByRole('button', { name: 'Copy person id' })).toBeVisible();
  await dialog.getByRole('button', { name: 'Close' }).click();
  await expect(dialog).toBeHidden();

  await page.getByLabel('Search people').fill(`nobody ${stamp}`);
  await expect(page.getByText(`No one matches nobody ${stamp}.`)).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Unmatched logins' })).toBeVisible();
});
