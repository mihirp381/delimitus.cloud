import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { expect, test } from '@playwright/test';

const token = process.env.SSC_E2E_TOKEN ?? '';
const auth = { Authorization: `Bearer ${token}` };

test('the requester sees their request but cannot decide it; the audit log filters and exports', async ({ page, request }) => {
  expect(token, 'run through e2e/run.mjs, which mints the token').not.toBe('');
  const stamp = Date.now().toString(36);
  const slug = `e2e-audit-${stamp}`;
  const connection = `warehouse-${stamp}`;

  // An app, and a request to connect a data source to its prod environment.
  const created = await request.post('/v1/apps', {
    headers: { ...auth, 'Idempotency-Key': randomUUID() },
    data: { slug },
  });
  expect(created.status()).toBe(201);
  const app = await created.json();
  const detail = await (await request.get(`/v1/apps/${app.id}`, { headers: auth })).json();
  const prod = detail.environments.find((e: { name: string }) => e.name === 'prod');
  const asked = await request.post('/v1/approvals', {
    headers: { ...auth, 'Idempotency-Key': randomUUID() },
    data: { environment_id: prod.id, kind: 'connect_data_source', subject_key: connection },
  });
  expect(asked.status()).toBe(201);
  const approval = await asked.json();

  await page.goto('/approvals');
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel('API token').fill(token);
  await page.getByRole('button', { name: 'Sign in' }).click();

  // Your own request is listed under all requests as pending, and you cannot decide it.
  await expect(page.getByRole('heading', { name: 'Approvals' })).toBeVisible();
  await page.getByRole('link', { name: 'All requests' }).click();
  const row = page.getByRole('row').filter({ hasText: connection });
  await expect(row).toContainText('pending');
  await expect(row).toContainText('Waiting for an approver');
  await expect(row.getByRole('link', { name: slug })).toBeVisible();
  await expect(row).toContainText('prod');
  await row.getByRole('link', { name: 'Open' }).click();
  await expect(page.getByRole('button', { name: /approve|reject/i })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Withdraw this request' })).toBeVisible();

  // The audit log finds the request by action and target.
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Audit log' }).click();
  const filters = page.getByRole('form', { name: 'Filter events' });
  await filters.getByLabel('Action').selectOption('approval.requested');
  await filters.getByLabel('Target id').fill(approval.id);
  await filters.getByRole('button', { name: 'Search' }).click();
  await expect(page).toHaveURL(new RegExp(`target_id=${approval.id}`));
  await expect(page.getByText('1 event', { exact: true })).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: 'approval.requested' })).toContainText(approval.id);

  // Export downloads the matching events as a file.
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export CSV' }).click();
  const file = await download;
  expect(file.suggestedFilename()).toMatch(/^audit-org_[a-z0-9]+\.csv$/);
  const csv = await readFile(await file.path(), 'utf8');
  expect(csv).toContain('approval.requested');
  expect(csv).toContain(approval.id);
  await expect(page.getByRole('status')).toHaveText(`Saved ${file.suggestedFilename()}.`);

  // The export is itself in the log.
  await filters.getByLabel('Action').selectOption('audit.exported');
  await filters.getByLabel('Target id').fill('');
  await filters.getByRole('button', { name: 'Search' }).click();
  await expect(page.getByRole('row').filter({ hasText: 'audit.exported' }).first()).toBeVisible();

  // A JSON Lines export of the whole log: its hash chain, as the real API wrote it, checks out in
  // the browser from the first event to the last row of the file that was saved.
  await filters.getByRole('button', { name: 'Clear filters' }).click();
  await expect(page).not.toHaveURL(/action=/);
  const chainDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export JSON Lines' }).click();
  const chainFile = await chainDownload;
  expect(chainFile.suggestedFilename()).toMatch(/^audit-org_[a-z0-9]+\.jsonl$/);
  const check = page.getByRole('status', { name: 'Hash chain check' });
  await expect(check).toContainText(/Chain intact: \d+ events of org_[a-z0-9]+, seq 1 to \d+\./);
  await expect(check).toContainText('every link from the first event was checked');
  const rows = (await readFile(await chainFile.path(), 'utf8')).trimEnd().split('\n');
  expect(JSON.parse(rows[0] ?? '{}').seq).toBe(1);
  await expect(check).toContainText(`Last hash ${JSON.parse(rows.at(-1) ?? '{}').hash}`);
});
