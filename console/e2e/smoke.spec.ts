import { randomUUID } from 'node:crypto';
import { expect, type APIRequestContext, test } from '@playwright/test';

const token = process.env.SSC_E2E_TOKEN ?? '';
const auth = { Authorization: `Bearer ${token}` };

async function grantsOf(request: APIRequestContext, path: string) {
  const response = await request.get(path, { headers: auth });
  expect(response.status()).toBe(200);
  return { etag: response.headers().etag ?? '', body: await response.json() };
}

test('sign in, find an app, and remove access through a stale version', async ({ page, request }) => {
  expect(token, 'run through e2e/run.mjs, which mints the token').not.toBe('');
  const slug = `e2e-${Date.now().toString(36)}`;

  // An app created through the API, shared with the org and its owner on prod.
  const created = await request.post('/v1/apps', {
    headers: { ...auth, 'Idempotency-Key': randomUUID() },
    data: { slug },
  });
  expect(created.status()).toBe(201);
  const app = await created.json();
  const detail = await (await request.get(`/v1/apps/${app.id}`, { headers: auth })).json();
  const prod = detail.environments.find((e: { name: string }) => e.name === 'prod');
  const grantsPath = `/v1/apps/${app.id}/environments/${prod.id}/grants`;
  const initial = await grantsOf(request, grantsPath);
  const shared = await request.put(grantsPath, {
    headers: { ...auth, 'If-Match': initial.etag },
    data: {
      grants: [
        { role: 'user', subject_kind: 'org' },
        { role: 'builder', subject_kind: 'user', subject_id: app.owner_user_id },
      ],
    },
  });
  expect(shared.status()).toBe(200);
  const seenByPage = shared.headers().etag;

  // Log in with a token.
  await page.goto('/');
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel('API token').fill(token);
  await page.getByRole('button', { name: 'Sign in' }).click();

  // The inventory shows the app and its owner by name; the shell names the org and the person.
  await expect(page.getByRole('heading', { name: 'Apps' })).toBeVisible();
  await expect(page.locator('.sidebar-org')).toHaveText('Organisation Dev org');
  await expect(page.getByRole('button', { name: /^Signed in as Dev Admin,/ })).toBeVisible();
  const row = page.getByRole('row').filter({ has: page.getByRole('link', { name: slug }) });
  await expect(row).toContainText('Dev Admin');
  await expect(row).not.toContainText(app.owner_user_id);
  await page.getByRole('link', { name: slug }).click();

  // App detail lists prod and preview.
  await expect(page.getByRole('heading', { name: slug, level: 1 })).toBeVisible();
  const prodPanel = page.getByRole('region', { name: 'Production prod' });
  await expect(prodPanel).toBeVisible();
  await expect(page.getByRole('region', { name: 'Preview preview' })).toBeVisible();
  await expect(prodPanel.getByText('Everyone in the organisation')).toBeVisible();

  // Who has access is its own tab, which reads production's sharing as it opens.
  await page.getByRole('navigation', { name: 'App sections' }).getByRole('link', { name: 'Access' }).click();
  await expect(page).toHaveURL(/tab=access/);
  const access = page.getByRole('region', { name: 'Who has access' });
  await expect(access.getByRole('table')).toContainText('Everyone in the organisation');
  await expect(access.getByRole('table')).toContainText('Dev Admin');

  // Someone else changes prod's sharing after the page read it.
  const current = await grantsOf(request, grantsPath);
  expect(current.etag).toBe(seenByPage);
  const meanwhile = await request.put(grantsPath, {
    headers: { ...auth, 'If-Match': current.etag },
    data: { grants: [{ role: 'user', subject_kind: 'org' }] },
  });
  expect(meanwhile.status()).toBe(200);
  const latest = meanwhile.headers().etag;

  // Removing access sends a PUT with If-Match and survives the 412.
  const puts: { ifMatch: string | undefined; status: number }[] = [];
  page.on('response', (response) => {
    const req = response.request();
    if (req.method() === 'PUT' && new URL(response.url()).pathname === grantsPath) {
      puts.push({ ifMatch: req.headers()['if-match'], status: response.status() });
    }
  });
  await access.getByRole('button', { name: /Remove access for Everyone in the organisation/ }).click();
  const dialog = page.getByRole('dialog', { name: 'Remove access' });
  const confirm = dialog.getByRole('button', { name: 'Remove access' });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel(/Type/).fill(slug);
  await confirm.click();
  await expect(page.getByRole('region', { name: 'Notifications' })).toContainText('Removed access');
  await expect(access.getByText('Nobody has access yet.')).toBeVisible();
  expect(puts).toEqual([
    { ifMatch: seenByPage, status: 412 },
    { ifMatch: latest, status: 200 },
  ]);

  const final = await grantsOf(request, grantsPath);
  expect(final.body.grants).toEqual([]);
});

test('walk an app from the overview to each panel of Settings', async ({ page, request }) => {
  expect(token, 'run through e2e/run.mjs, which mints the token').not.toBe('');
  const slug = `e2e-walk-${Date.now().toString(36)}`;
  const created = await request.post('/v1/apps', {
    headers: { ...auth, 'Idempotency-Key': randomUUID() },
    data: { slug },
  });
  expect(created.status()).toBe(201);
  const app = await created.json();

  await page.goto(`/apps/${app.id}`);
  await page.getByLabel('API token').fill(token);
  await page.getByLabel('Keep it for this tab').check();
  await page.getByRole('button', { name: 'Sign in' }).click();

  const sections = page.getByRole('navigation', { name: 'App sections' });
  await expect(sections.getByRole('link')).toHaveText(['Overview', 'Logs', 'Access', 'Settings']);
  const prod = page.getByRole('region', { name: 'Production prod' });
  const preview = page.getByRole('region', { name: 'Preview preview' });
  await expect(prod.locator('.btn-primary')).toHaveCount(1);
  await expect(prod.getByRole('button', { name: 'Share Production' })).toHaveClass(/btn-primary/);
  await expect(preview.locator('.btn-primary')).toHaveCount(1);
  await expect(preview.getByRole('button', { name: 'Promote preview to production' })).toHaveClass(/btn-primary/);
  await expect(prod.getByText('Config version')).toBeHidden();

  await sections.getByRole('link', { name: 'Settings', exact: true }).click();
  await expect(page).toHaveURL(/tab=settings/);
  const panels = ['Secrets', 'Timers', 'Repository', 'Admin actions'];
  for (const name of panels) {
    await expect(page.getByRole('region', { name, exact: true })).toBeVisible();
  }
  await expect(page.locator('main h2')).toHaveText(panels);

  await page.goto(`/apps/${app.id}?tab=timers`);
  await expect(page).toHaveURL(new RegExp(`/apps/${app.id}\\?tab=settings#timers$`));
  await expect(page.getByRole('region', { name: 'Timers', exact: true })).toBeVisible();
});
