import { randomUUID } from 'node:crypto';
import { expect, type APIRequestContext, type Page, test } from '@playwright/test';

const token = process.env.SSC_E2E_TOKEN ?? '';
const operatorToken = process.env.SSC_E2E_OPERATOR_TOKEN ?? '';
const apiUrl = process.env.SSC_API_URL ?? '';
const auth = { Authorization: `Bearer ${token}` };

async function createApp(request: APIRequestContext, slug: string) {
  const created = await request.post('/v1/apps', {
    headers: { ...auth, 'Idempotency-Key': randomUUID() },
    data: { slug },
  });
  expect(created.status()).toBe(201);
  const app = await created.json();
  const detail = await (await request.get(`/v1/apps/${app.id}`, { headers: auth })).json();
  const prod = detail.environments.find((e: { name: string }) => e.name === 'prod');
  return { id: app.id as string, prodGrants: `/v1/apps/${app.id}/environments/${prod.id}/grants` };
}

async function operator(request: APIRequestContext, method: 'post' | 'put', path: string, data: object) {
  const response = await request[method](`${apiUrl}/internal/v1${path}`, {
    headers: { Authorization: `Bearer ${operatorToken}`, 'Idempotency-Key': randomUUID() },
    data,
  });
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

async function signIn(page: Page, path: string) {
  await page.goto(path);
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel('API token').fill(token);
  await page.getByRole('button', { name: 'Sign in' }).click();
}

test('share production with a group found by name and a person found by email', async ({ page, request }) => {
  expect(token && operatorToken, 'run through e2e/run.mjs, which mints the tokens').toBeTruthy();
  const stamp = Date.now().toString(36);
  const slug = `e2e-share-${stamp}`;
  const team = `E2E Team ${stamp}`;
  const email = `pat-${stamp}@example.com`;

  // A person and a group in the directory, pushed as the operator would.
  const person = await operator(request, 'post', '/directory/users', {
    issuer: 'https://dev.invalid',
    subject: `pat-${stamp}`,
    display_name: 'Pat E2E',
    email,
    role: 'member',
    status: 'active',
  });
  const group = await operator(request, 'post', '/directory/groups', {
    directory_ref: `ref-${stamp}`,
    display_name: team,
  });
  await operator(request, 'put', `/directory/groups/${group.group_id}/members`, {
    user_ids: [person.user_id],
  });
  const app = await createApp(request, slug);

  await signIn(page, `/apps/${app.id}`);
  const prod = page.getByRole('region', { name: 'Production prod' });
  await expect(prod.getByText('Nobody has access yet.')).toBeVisible();

  // Someone else shares with the org after the page read the rules: the dialog's PUT meets a 412.
  const current = await request.get(app.prodGrants, { headers: auth });
  const meanwhile = await request.put(app.prodGrants, {
    headers: { ...auth, 'If-Match': current.headers().etag ?? '' },
    data: { grants: [{ role: 'user', subject_kind: 'org' }] },
  });
  expect(meanwhile.status()).toBe(200);
  const puts: number[] = [];
  page.on('response', (response) => {
    const req = response.request();
    if (req.method() === 'PUT' && new URL(response.url()).pathname === app.prodGrants) {
      puts.push(response.status());
    }
  });

  await prod.getByRole('button', { name: 'Share Production' }).click();
  let dialog = page.getByRole('dialog', { name: 'Share production' });
  await dialog.getByLabel('Group name or grp_ id').fill(team.toUpperCase());
  await dialog.getByRole('button', { name: 'Find' }).click();
  await expect(dialog.getByRole('radio', { name: new RegExp(team) })).toBeChecked();
  await expect(dialog.getByText('1 active member')).toBeVisible();
  await dialog.getByRole('button', { name: 'Share', exact: true }).click();
  await expect(prod.getByRole('status')).toHaveText(`Shared production with ${team} (user).`);
  expect(puts).toEqual([412, 200]);

  await prod.getByRole('button', { name: 'Share Production' }).click();
  dialog = page.getByRole('dialog', { name: 'Share production' });
  await dialog.getByRole('radio', { name: 'A person' }).check();
  await dialog.getByLabel('Email or usr_ id').fill(email);
  await dialog.getByLabel('Email or usr_ id').press('Enter');
  await expect(dialog.getByRole('radio', { name: /Pat E2E/ })).toBeChecked();
  await dialog.getByLabel('Role').selectOption('builder');
  await dialog.getByRole('button', { name: 'Share', exact: true }).click();
  await expect(prod.getByRole('status')).toHaveText('Shared production with Pat E2E (builder).');

  const final = await (await request.get(app.prodGrants, { headers: auth })).json();
  const keys = final.grants.map((g: { role: string; subject_kind: string; subject_id: string | null }) =>
    [g.role, g.subject_kind, g.subject_id].join(':'),
  );
  expect(keys.sort()).toEqual(
    [`builder:user:${person.user_id}`, `user:group:${group.group_id}`, 'user:org:'].sort(),
  );
});

test('disable an app with the kill switch, follow the run, and enable it again', async ({ page, request }) => {
  expect(token, 'run through e2e/run.mjs, which mints the token').not.toBe('');
  const slug = `e2e-kill-${Date.now().toString(36)}`;
  const app = await createApp(request, slug);

  await signIn(page, `/apps/${app.id}`);
  const head = page.locator('.page-head');
  await expect(head.getByText('active')).toBeVisible();
  const admin = page.getByRole('region', { name: 'Admin actions' });

  const posts: { status: number; runId: string }[] = [];
  page.on('response', async (response) => {
    const req = response.request();
    if (req.method() === 'POST' && new URL(response.url()).pathname === `/v1/apps/${app.id}/kill-switch`) {
      posts.push({ status: response.status(), runId: (await response.json()).run_id });
    }
  });

  await admin.getByRole('button', { name: 'Disable' }).click();
  const dialog = page.getByRole('dialog', { name: `Disable ${slug}` });
  const confirm = dialog.getByRole('button', { name: 'Disable' });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel(/Type/).fill(slug);
  await confirm.click();
  await expect(head.getByText('disabled')).toBeVisible();

  const run = admin.getByRole('group', { name: 'Kill switch run' });
  await expect(run.getByText('Deny at the gateway')).toBeVisible();
  await expect(run.locator('p .badge')).toHaveText(/completed|failed/, { timeout: 60_000 });
  await expect(run.getByText('Pause timers')).toBeVisible();
  expect(posts).toHaveLength(1);
  expect(posts[0]?.status).toBe(202);
  await expect(run.getByText(posts[0]?.runId ?? 'missing')).toBeVisible();

  const polled = await request.get(`/v1/apps/${app.id}/kill-switch/${posts[0]?.runId}`, { headers: auth });
  expect(polled.status()).toBe(200);
  const body = await polled.json();
  expect(body.steps.map((s: { name: string }) => s.name)).toEqual([
    'gateway_deny',
    'datagw_suspend',
    'egress_remove',
    'scale_to_zero',
    'pause_timers',
  ]);
  await expect(run.locator('p .badge')).toHaveText(body.state);

  await admin.getByRole('button', { name: 'Enable' }).click();
  await expect(admin.getByRole('status')).toContainText(`${slug} is active again`);
  await expect(head.getByText('active')).toBeVisible();
  await expect(admin.getByRole('button', { name: 'Disable' })).toBeVisible();
});
