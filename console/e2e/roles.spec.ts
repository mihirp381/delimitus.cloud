import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { type APIRequestContext, expect, type Page, test } from '@playwright/test';

// The role matrix (GA-10.2) against the real API: an org admin, a member who is a builder on one
// app's production, and a member who is only a user of it. The console hides what a role cannot
// use; the API is what refuses, so each hidden thing is also asked for directly.

const adminToken = process.env.SSC_E2E_TOKEN ?? '';
const operatorToken = process.env.SSC_E2E_OPERATOR_TOKEN ?? '';
const apiUrl = process.env.SSC_API_URL ?? '';
const composeProject = process.env.SSC_E2E_COMPOSE_PROJECT ?? '';
const composeFile = process.env.SSC_E2E_COMPOSE_FILE ?? '';

const EVERYONE = ['Apps', 'Approvals', 'Connections', 'Internet access'];
const ADMIN_ONLY = ['Your environment', 'Audit log'];
const FORBIDDEN = 'Your credential is valid but does not allow this action.';

interface Person {
  readonly id: string;
  readonly token: string;
}

interface World {
  readonly slug: string;
  readonly appId: string;
  readonly grants: string;
  readonly builder: Person;
  readonly user: Person;
  readonly colleagueId: string;
}

const bearer = (token: string) => ({ Authorization: `Bearer ${token}` });

/** A token for a person in the directory, signed by the dev stack inside the API's container. */
function mint(userId: string): string {
  const args = ['compose', '-p', composeProject, '-f', composeFile, 'exec', '-T', 'api'];
  const token = ['python', 'tools/dev_stack.py', '--dir', '/state', 'token', '--sub', userId];
  return execFileSync('docker', [...args, ...token], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] }).trim();
}

async function addPerson(request: APIRequestContext, name: string, stamp: string): Promise<string> {
  const response = await request.post(`${apiUrl}/internal/v1/directory/users`, {
    headers: { ...bearer(operatorToken), 'Idempotency-Key': randomUUID() },
    data: {
      issuer: 'https://dev.invalid',
      subject: `${name}-${stamp}`,
      display_name: `${name} E2E`,
      email: `${name}-${stamp}@example.com`,
      role: 'member',
      status: 'active',
    },
  });
  expect(response.status(), await response.text()).toBe(200);
  return (await response.json()).user_id;
}

let made: Promise<World> | undefined;

/** One app shared with a builder and a user, made once for the three tests. */
function world(request: APIRequestContext): Promise<World> {
  made ??= (async () => {
    expect(adminToken && operatorToken && composeProject, 'run through e2e/run.mjs').toBeTruthy();
    const stamp = Date.now().toString(36);
    const slug = `e2e-roles-${stamp}`;
    const [builderId, userId, colleagueId] = await Promise.all(
      ['builder', 'reader', 'colleague'].map((name) => addPerson(request, name, stamp)),
    );
    const created = await request.post('/v1/apps', {
      headers: { ...bearer(adminToken), 'Idempotency-Key': randomUUID() },
      data: { slug },
    });
    expect(created.status()).toBe(201);
    const app = await created.json();
    const detail = await (await request.get(`/v1/apps/${app.id}`, { headers: bearer(adminToken) })).json();
    const prod = detail.environments.find((e: { name: string }) => e.name === 'prod');
    const grants = `/v1/apps/${app.id}/environments/${prod.id}/grants`;
    const current = await request.get(grants, { headers: bearer(adminToken) });
    const shared = await request.put(grants, {
      headers: { ...bearer(adminToken), 'If-Match': current.headers().etag ?? '' },
      data: {
        grants: [
          { role: 'builder', subject_kind: 'user', subject_id: builderId },
          { role: 'user', subject_kind: 'user', subject_id: userId },
        ],
      },
    });
    expect(shared.status(), await shared.text()).toBe(200);
    return {
      slug,
      appId: app.id,
      grants,
      builder: { id: builderId ?? '', token: mint(builderId ?? '') },
      user: { id: userId ?? '', token: mint(userId ?? '') },
      colleagueId: colleagueId ?? '',
    };
  })();
  return made;
}

/** Signs in with the dev login. */
async function signIn(page: Page, path: string, token: string) {
  await page.goto(path);
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel('API token').fill(token);
  // Kept for the tab, so that loading an address afresh stays signed in.
  await page.getByLabel('Keep it for this tab').check();
  await page.getByRole('button', { name: 'Sign in' }).click();
}

const nav = (page: Page) => page.getByRole('navigation', { name: 'Main' }).getByRole('link');

/** What both kinds of member see: no admin screens in the nav or by address, and no admin actions. */
async function seesNothingAdmin(page: Page, request: APIRequestContext, w: World, person: Person) {
  await expect(nav(page)).toHaveText(EVERYONE);

  // The app list is the plain one, not the admin inventory, and the shared app is in it.
  await page.goto('/');
  await expect(page.getByRole('link', { name: w.slug })).toBeVisible();
  await expect(page.getByRole('columnheader')).toHaveText(['App', 'Status', 'Owner', 'App id']);
  await expect(page.getByLabel('Environment')).toHaveCount(0);

  // Typing an admin screen's address shows a line, not the screen, and the API refuses the read.
  await page.goto('/audit');
  await expect(page.getByText('Only org admins can see the audit log.')).toBeVisible();
  await expect(page.getByRole('form', { name: 'Filter events' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /Export/ })).toHaveCount(0);
  await page.goto('/environment');
  await expect(page.getByText('Only org admins can see the environment.')).toBeVisible();
  await expect(nav(page)).toHaveText(EVERYONE);
  for (const path of ['/v1/audit', '/v1/audit/export?format=csv', '/v1/inventory', '/v1/usage', '/v1/cell']) {
    const refused = await request.get(path, { headers: bearer(person.token) });
    expect(refused.status(), path).toBe(403);
  }

  // The app page has no admin action, and the API refuses the kill switch.
  await page.goto(`/apps/${w.appId}`);
  const admin = page.getByRole('region', { name: 'Admin actions' });
  await expect(admin).toContainText('Only an org admin can disable, quarantine, enable or transfer this app.');
  await expect(admin.getByRole('button')).toHaveCount(0);
  const pulled = await request.post(`/v1/apps/${w.appId}/kill-switch`, {
    headers: { ...bearer(person.token), 'Idempotency-Key': randomUUID() },
    data: { mode: 'disable' },
  });
  expect(pulled.status()).toBe(403);
}

test('an org admin sees every screen and the admin actions', async ({ page, request }) => {
  const w = await world(request);
  await signIn(page, '/', adminToken);
  await expect(nav(page)).toHaveText([...EVERYONE, ...ADMIN_ONLY]);

  // The inventory, with its environment filter.
  await expect(page.getByRole('link', { name: w.slug })).toBeVisible();
  await expect(page.getByLabel('Environment')).toBeVisible();

  await nav(page).filter({ hasText: 'Audit log' }).click();
  await expect(page.getByRole('form', { name: 'Filter events' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Export JSON Lines' })).toBeVisible();
  await nav(page).filter({ hasText: 'Your environment' }).click();
  await expect(page.getByRole('heading', { name: 'Your environment' })).toBeVisible();
  await expect(page.getByText('Only org admins can see the environment.')).toHaveCount(0);

  await page.goto(`/apps/${w.appId}`);
  const admin = page.getByRole('region', { name: 'Admin actions' });
  await expect(admin.getByRole('button', { name: 'Disable' })).toBeVisible();
  await expect(admin.getByRole('button', { name: 'Quarantine' })).toBeVisible();
  const prod = page.getByRole('region', { name: 'Production prod' });
  await expect(prod.getByRole('button', { name: 'Share Production' })).toBeVisible();
  await expect(prod.getByRole('button', { name: 'Roll back Production' })).toBeEnabled();
});

test('a builder sees nothing admin, and can share and open rollback on their app', async ({ page, request }) => {
  const w = await world(request);
  await signIn(page, '/', w.builder.token);
  await expect(page.getByText(w.builder.id)).toBeVisible();
  await seesNothingAdmin(page, request, w, w.builder);

  // Share: the API takes a builder's change.
  const prod = page.getByRole('region', { name: 'Production prod' });
  await prod.getByRole('button', { name: 'Share Production' }).click();
  const share = page.getByRole('dialog', { name: 'Share production' });
  await share.getByRole('radio', { name: 'A person' }).check();
  // A member cannot look people up by email, so the person is named by id.
  await share.getByLabel('User id').fill(w.colleagueId);
  await share.getByRole('button', { name: 'Share', exact: true }).click();
  await expect(prod.getByRole('status')).toHaveText(`Shared production with ${w.colleagueId} (user).`);
  const after = await (await request.get(w.grants, { headers: bearer(adminToken) })).json();
  expect(after.grants.map((g: { subject_id: string | null }) => g.subject_id)).toContain(w.colleagueId);

  // Rollback: the dialog opens and reads the releases without a refusal.
  await prod.getByRole('button', { name: 'Roll back Production' }).click();
  const rollback = page.getByRole('dialog', { name: 'Roll back production' });
  await expect(rollback).toBeVisible();
  await expect(rollback.getByRole('alert')).toHaveCount(0);
  await expect(rollback.getByText(/release/i).first()).toBeVisible();
});

test('a user sees nothing admin, and the API refuses the actions shown to everyone', async ({ page, request }) => {
  const w = await world(request);
  await signIn(page, '/', w.user.token);
  await expect(page.getByText(w.user.id)).toBeVisible();
  await seesNothingAdmin(page, request, w, w.user);

  // Share is shown to everyone; the API's refusal is shown as it came.
  const prod = page.getByRole('region', { name: 'Production prod' });
  const before = await (await request.get(w.grants, { headers: bearer(adminToken) })).json();
  await prod.getByRole('button', { name: 'Share Production' }).click();
  const share = page.getByRole('dialog', { name: 'Share production' });
  await share.getByRole('radio', { name: 'Everyone in the organisation' }).check();
  await share.getByRole('button', { name: 'Share', exact: true }).click();
  await expect(share.getByRole('alert')).toContainText(FORBIDDEN);
  await expect(share.getByRole('alert')).toContainText('FORBIDDEN');
  const after = await (await request.get(w.grants, { headers: bearer(adminToken) })).json();
  expect(after.grants).toEqual(before.grants);
  await share.getByRole('button', { name: 'Cancel' }).click();

  // So is rollback: a user may not read the releases.
  await prod.getByRole('button', { name: 'Roll back Production' }).click();
  const rollback = page.getByRole('dialog', { name: 'Roll back production' });
  await expect(rollback.getByRole('alert').first()).toContainText(FORBIDDEN);
});
