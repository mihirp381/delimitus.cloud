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

  // The inventory shows the app.
  await expect(page.getByRole('heading', { name: 'Apps' })).toBeVisible();
  await page.getByRole('link', { name: slug }).click();

  // App detail lists prod and preview.
  await expect(page.getByRole('heading', { name: slug, level: 1 })).toBeVisible();
  const prodPanel = page.getByRole('region', { name: 'Production prod' });
  await expect(prodPanel).toBeVisible();
  await expect(page.getByRole('region', { name: 'Preview preview' })).toBeVisible();
  await expect(prodPanel.getByText('Everyone in the organisation')).toBeVisible();

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
  await prodPanel.getByRole('button', { name: /Remove access for Everyone in the organisation/ }).click();
  const dialog = page.getByRole('dialog', { name: 'Remove access' });
  const confirm = dialog.getByRole('button', { name: 'Remove access' });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel(/Type/).fill(slug);
  await confirm.click();
  await expect(prodPanel.getByRole('status')).toContainText('Removed access');
  await expect(prodPanel.getByText('Nobody has access yet.')).toBeVisible();
  expect(puts).toEqual([
    { ifMatch: seenByPage, status: 412 },
    { ifMatch: latest, status: 200 },
  ]);

  const final = await grantsOf(request, grantsPath);
  expect(final.body.grants).toEqual([]);
});
