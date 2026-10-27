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
const ADMIN_ONLY = ['People', 'Audit log', 'Usage'];
const HELP = 'Help';
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
  readonly builderEmail: string;
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
      builderEmail: `builder-${stamp}@example.com`,
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

/** What a finished action said: the toast region, which every page of the shell has. */
const said = (page: Page) => page.getByRole('region', { name: 'Notifications' });

/** Opens one tab of the app page that is on show. */
const openTab = (page: Page, name: string) =>
  page.getByRole('navigation', { name: 'App sections' }).getByRole('link', { name, exact: true }).click();

/** Help is for every role: it reads the API on the console's own origin and says it answers. */
async function seesHelp(page: Page) {
  await nav(page).filter({ hasText: HELP }).click();
  await expect(page.getByRole('heading', { name: 'Help', level: 1 })).toBeVisible();
  const status = page.getByRole('status', { name: 'SSC status' });
  await expect(status).toContainText('SSC is answering');
  await expect(status.locator('time')).toHaveAttribute('datetime', /^\d{4}-\d\d-\d\dT/);
  await expect(page.getByRole('button', { name: 'Check again' })).toBeEnabled();
}

/** What both kinds of member see: no admin screens in the nav or by address, and no admin actions. */
async function seesNothingAdmin(page: Page, request: APIRequestContext, w: World, person: Person) {
  await expect(nav(page)).toHaveText([...EVERYONE, HELP]);

  // The app list is the plain one, not the admin inventory, and the shared app is in it.
  await page.goto('/');
  await expect(page.getByRole('link', { name: w.slug })).toBeVisible();
  await expect(page.getByRole('columnheader')).toHaveText(['App', 'Owner']);
  await expect(page.getByLabel('Environment')).toHaveCount(0);

  // Typing an admin screen's address shows a line, not the screen, and the API refuses the read.
  await page.goto('/audit');
  await expect(page.getByText('Only org admins can see the audit log.')).toBeVisible();
  await expect(page.getByRole('form', { name: 'Filter events' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /Export/ })).toHaveCount(0);
  await page.goto('/environment');
  await expect(page.getByText('Only org admins can see usage.')).toBeVisible();
  await page.goto('/people');
  await expect(page.getByText('Only org admins can look people up and link logins.')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Unmatched logins' })).toHaveCount(0);
  await expect(page.getByRole('form')).toHaveCount(0);
  await expect(nav(page)).toHaveText([...EVERYONE, HELP]);
  const adminReads = [
    '/v1/audit',
    '/v1/audit/export?format=csv',
    '/v1/inventory',
    '/v1/usage',
    '/v1/cell',
    '/v1/unlinked-logins',
    `/v1/users?email=${encodeURIComponent(w.builderEmail)}`,
  ];
  for (const path of adminReads) {
    const refused = await request.get(path, { headers: bearer(person.token) });
    expect(refused.status(), path).toBe(403);
  }

  await seesHelp(page);

  // The app page has no admin action, and the API refuses the kill switch and a delete.
  await page.goto(`/apps/${w.appId}?tab=settings`);
  const admin = page.getByRole('region', { name: 'Admin actions' });
  await expect(admin).toContainText(
    'Only an org admin can disable, quarantine, enable, transfer or delete this app.',
  );
  await expect(admin.getByRole('button')).toHaveCount(0);
  const pulled = await request.post(`/v1/apps/${w.appId}/kill-switch`, {
    headers: { ...bearer(person.token), 'Idempotency-Key': randomUUID() },
    data: { mode: 'disable' },
  });
  expect(pulled.status()).toBe(403);
  const deleted = await request.post(`/v1/apps/${w.appId}/delete`, {
    headers: { ...bearer(person.token), 'Idempotency-Key': randomUUID() },
    data: { confirm: w.slug },
  });
  expect(deleted.status()).toBe(403);
  await openTab(page, 'Overview');
}

test('an org admin sees every screen and the admin actions', async ({ page, request }) => {
  const w = await world(request);
  await signIn(page, '/', adminToken);
  await expect(nav(page)).toHaveText([...EVERYONE, ...ADMIN_ONLY, HELP]);
  // Eight links, the count outside the Approvals link: every one is on show, none below the fold.
  await expect(nav(page).first()).toBeInViewport({ ratio: 1 });
  await expect(nav(page).last()).toBeInViewport({ ratio: 1 });

  // The inventory, with its environment filter.
  await expect(page.getByRole('link', { name: w.slug })).toBeVisible();
  await expect(page.getByLabel('Environment')).toBeVisible();

  await nav(page).filter({ hasText: 'Audit log' }).click();
  await expect(page.getByRole('form', { name: 'Filter events' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Export JSON Lines' })).toBeVisible();
  await nav(page).filter({ hasText: 'Usage' }).click();
  await expect(page.getByRole('heading', { name: 'Usage' })).toBeVisible();
  await expect(page.getByText('Only org admins can see usage.')).toHaveCount(0);

  // People: the unmatched logins (none here, every person was pushed by the operator), and a
  // person found by their whole email address.
  await nav(page).filter({ hasText: 'People' }).click();
  await expect(page.getByText('Every login so far matched a person.')).toBeVisible();
  const person = page.getByRole('form', { name: 'Look up a person' });
  await person.getByLabel('Email address').fill(w.builderEmail.toUpperCase());
  await person.getByRole('button', { name: 'Find' }).click();
  const found = page.getByRole('table', { name: /People with the email/ }).getByRole('row').nth(1);
  await expect(found.getByRole('cell')).toHaveText([`builder E2E ${w.builder.id}`, w.builderEmail, 'member', 'active']);
  const group = page.getByRole('form', { name: 'Look up a group' });
  await group.getByLabel('Group name').fill(`No such group ${w.slug}`);
  await group.getByRole('button', { name: 'Find' }).click();
  await expect(page.getByText(`No group is named exactly No such group ${w.slug}.`)).toBeVisible();

  await seesHelp(page);

  await page.goto(`/apps/${w.appId}?tab=settings`);
  const admin = page.getByRole('region', { name: 'Admin actions' });
  await expect(admin.getByRole('button', { name: 'Disable' })).toBeVisible();
  await expect(admin.getByRole('button', { name: 'Quarantine' })).toBeVisible();
  await expect(admin.getByRole('button', { name: 'Delete app' })).toBeVisible();
  await openTab(page, 'Overview');
  const prod = page.getByRole('region', { name: 'Production prod' });
  await expect(prod.getByRole('button', { name: 'Share Production' })).toBeVisible();
  await expect(prod.getByRole('button', { name: 'Roll back Production' })).toBeEnabled();
});

test('a builder sees nothing admin, and can share and open rollback on their app', async ({ page, request }) => {
  const w = await world(request);
  await signIn(page, '/', w.builder.token);
  await expect(page.getByRole('button', { name: /^Signed in as builder E2E,/ })).toBeVisible();
  await seesNothingAdmin(page, request, w, w.builder);

  // Share: the API takes a builder's change.
  const prod = page.getByRole('region', { name: 'Production prod' });
  await prod.getByRole('button', { name: 'Share Production' }).click();
  const share = page.getByRole('dialog', { name: 'Share production' });
  await share.getByRole('radio', { name: 'A person' }).check();
  // A member cannot look people up by email, so the person is named by id.
  await share.getByLabel('User id').fill(w.colleagueId);
  await share.getByRole('button', { name: 'Share', exact: true }).click();
  await expect(said(page).getByText(`Shared production with ${w.colleagueId} (user).`, { exact: true })).toBeVisible();
  const after = await (await request.get(w.grants, { headers: bearer(adminToken) })).json();
  expect(after.grants.map((g: { subject_id: string | null }) => g.subject_id)).toContain(w.colleagueId);

  // Rollback: the dialog opens and reads the releases without a refusal.
  await prod.getByRole('button', { name: 'Roll back Production' }).click();
  const rollback = page.getByRole('dialog', { name: 'Roll back production' });
  await expect(rollback).toBeVisible();
  await expect(rollback.getByRole('alert')).toHaveCount(0);
  await expect(rollback.getByText(/release/i).first()).toBeVisible();
  await rollback.getByRole('button', { name: 'Cancel' }).click();

  // Secrets: a builder may read the names and versions, and is shown the command to set one.
  await openTab(page, 'Secrets');
  const secrets = page.getByRole('region', { name: 'Secrets' });
  await expect(secrets.getByText(`Production of ${w.slug} has no secrets.`)).toBeVisible();
  await expect(secrets.getByLabel('Set a secret in production', { exact: true })).toHaveText(
    `ssc secret set ${w.slug} NAME --env prod`,
  );
});

test('a user sees nothing admin, and the API refuses the actions shown to everyone', async ({ page, request }) => {
  const w = await world(request);
  await signIn(page, '/', w.user.token);
  await expect(page.getByRole('button', { name: /^Signed in as reader E2E,/ })).toBeVisible();
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
  await rollback.getByRole('button', { name: 'Cancel' }).click();

  // And the secrets' names: the tab opens for everyone, and the API refuses a user.
  await openTab(page, 'Secrets');
  const secrets = page.getByRole('region', { name: 'Secrets' });
  await expect(secrets.getByRole('heading', { name: 'Set or rotate a secret' })).toBeVisible();
  await expect(secrets.getByRole('alert')).toContainText(FORBIDDEN);
});
