import { randomUUID } from 'node:crypto';
import { type APIRequestContext, expect, type Page, test } from '@playwright/test';

const token = process.env.SSC_E2E_TOKEN ?? '';
const operatorToken = process.env.SSC_E2E_OPERATOR_TOKEN ?? '';
const apiUrl = process.env.SSC_API_URL ?? '';
const auth = { Authorization: `Bearer ${token}` };

const toast = (page: Page, text: string) =>
  page.getByRole('region', { name: 'Notifications' }).getByText(text, { exact: false });

async function addPerson(request: APIRequestContext, name: string, stamp: string): Promise<string> {
  const email = `${name}-${stamp}@example.com`;
  const response = await request.post(`${apiUrl}/internal/v1/directory/users`, {
    headers: { Authorization: `Bearer ${operatorToken}`, 'Idempotency-Key': randomUUID() },
    data: {
      issuer: 'https://dev.invalid',
      subject: `${name}-${stamp}`,
      display_name: `${name} E2E`,
      email,
      role: 'member',
      status: 'active',
    },
  });
  expect(response.status(), await response.text()).toBe(200);
  return email;
}

test('add a connection with the first step only, change its owner and status, and link it', async ({ page, request }) => {
  expect(token && operatorToken, 'run through e2e/run.mjs').toBeTruthy();
  const stamp = Date.now().toString(36);
  const name = `ledger-${stamp}`;
  const slug = `e2e-conn-${stamp}`;
  const [first, second] = await Promise.all(['alpha', 'beta'].map((who) => addPerson(request, who, stamp)));
  const created = await request.post('/v1/apps', { headers: { ...auth, 'Idempotency-Key': randomUUID() }, data: { slug } });
  expect(created.status()).toBe(201);
  const app = await created.json();

  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto('/connections');
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel('API token').fill(token);
  await page.getByLabel('Keep it for this tab').check();
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Connections', level: 1 })).toBeVisible();

  await page.getByRole('button', { name: 'Add connection' }).click();
  const dialog = page.getByRole('dialog', { name: 'Add a connection' });
  await dialog.getByLabel(/^Name/).fill(name);
  await dialog.getByLabel('Owner: email or usr_ id').fill(first ?? '');
  await dialog.getByRole('button', { name: 'Find' }).click();
  await expect(dialog.getByRole('radio', { name: /alpha E2E/ })).toBeChecked();
  await dialog.getByLabel('Host', { exact: true }).fill('db.internal.example.com');
  await dialog.getByLabel('Database', { exact: true }).fill('ledger');
  await expect(dialog.getByLabel('Classification')).toBeHidden();
  await dialog.getByRole('button', { name: 'Add connection' }).click();
  await expect(toast(page, `Added ${name}.`)).toBeVisible();
  await expect(dialog).toBeHidden();

  const table = page.getByRole('table', { name: 'Connections' });
  const row = table.getByRole('row').filter({ hasText: name });
  await expect(row).toContainText('alpha E2E');
  await expect(row).toContainText('pending');
  expect(await table.getByRole('columnheader').allTextContents()).toEqual(['Name', 'Kind', 'State', 'Owner']);
  const overflow = await page.locator('.table-wrap').filter({ has: table }).evaluate('el => el.scrollWidth - el.clientWidth');
  expect(overflow).toBe(0);

  await row.getByRole('link', { name }).click();
  await expect(page.getByRole('heading', { name, level: 1 })).toBeVisible();
  for (const panel of ['Overview', 'Schemas', 'Limits and ceiling', 'Status']) {
    await expect(page.getByRole('region', { name: panel, exact: true })).toBeVisible();
  }

  const overview = page.getByRole('region', { name: 'Overview', exact: true });
  await overview.getByRole('button', { name: 'Change' }).click();
  await overview.getByLabel('Owner: email or usr_ id').fill(second ?? '');
  await overview.getByRole('button', { name: 'Find' }).click();
  await expect(overview.getByRole('radio', { name: /beta E2E/ })).toBeChecked();
  await overview.getByRole('button', { name: 'Save changes' }).click();
  await expect(toast(page, `Changed ${name}.`)).toBeVisible();
  await expect(overview).toContainText('beta E2E');

  const status = page.getByRole('region', { name: 'Status', exact: true });
  await status.getByRole('button', { name: 'Suspend this connection' }).click();
  const suspend = page.getByRole('dialog', { name: `Suspend ${name}` });
  await suspend.getByLabel(/to confirm/).fill(name);
  await suspend.getByRole('button', { name: 'Suspend' }).click();
  await expect(status).toContainText('Every query on it is refused.');
  await expect(page.locator('.page-title')).toContainText('suspended');
  await status.getByRole('button', { name: 'Resume this connection' }).click();
  await page.getByRole('dialog', { name: `Resume ${name}` }).getByRole('button', { name: 'Resume' }).click();
  await expect(status).toContainText('Linked apps can query it.');

  await page.goto(`/apps/${app.id}?tab=data&env=prod`);
  const data = page.getByRole('region', { name: 'Data connections' });
  await data.getByLabel('Connection to link').selectOption(name);
  await data.getByRole('button', { name: 'Link connection' }).click();
  await expect(toast(page, `Linked ${name}.`)).toBeVisible();
  await expect(data.getByRole('table', { name: 'Data connections of Production' })).toContainText(name);
});
