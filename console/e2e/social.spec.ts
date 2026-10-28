import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

const token = process.env.SSC_E2E_TOKEN ?? '';
const operatorToken = process.env.SSC_E2E_OPERATOR_TOKEN ?? '';
const apiUrl = process.env.SSC_API_URL ?? '';
const composeProject = process.env.SSC_E2E_COMPOSE_PROJECT ?? '';
const composeFile = process.env.SSC_E2E_COMPOSE_FILE ?? '';
const APP_DSN = `"$(python -c 'import json; print(json.load(open("/state/state.json"))["database_dsn"])')"`;

const bearer = (t: string) => ({ Authorization: `Bearer ${t}` });

function inApi(...command: string[]): string {
  const args = ['compose', '-p', composeProject, '-f', composeFile, 'exec', '-T', 'api', ...command];
  return execFileSync('docker', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] }).trim();
}

// set-sign-in refuses a directory org social mode, so the test writes the mode itself.
const FORCE_SOCIAL = [
  'import json, sys, psycopg',
  'from ssc_control.db import bind_org_sync',
  'with psycopg.connect(json.load(open("/state/state.json"))["database_dsn"]) as c:',
  '    bind_org_sync(c, sys.argv[1])',
  "    c.execute(\"update ssc.org set sign_in_mode = 'social', social_providers = '{google,github}', invite_domains = '{example.com}' where id = %s\", (sys.argv[1],))",
].join('\n');

function toSocial(org: string): void {
  inApi('python', '-c', FORCE_SOCIAL, org);
}

function toDirectory(org: string): void {
  const cli = `python -m ssc_control.identity set-sign-in --org ${org} --mode directory --operator op_e2e`;
  inApi('sh', '-c', `SSC_DATABASE_DSN=${APP_DSN} ${cli}`);
}

let org = '';

test.beforeAll(async ({ request }) => {
  expect(token && operatorToken && composeProject, 'run through e2e/run.mjs').toBeTruthy();
  org = (await (await request.get(`${apiUrl}/v1/whoami`, { headers: bearer(token) })).json()).org_id;
  toSocial(org);
});

test.afterAll(() => {
  if (org) toDirectory(org);
});

test('a social org invites by email and deactivates from the People page', async ({ page, request }) => {
  const stamp = Date.now().toString(36);
  const added = await request.post(`${apiUrl}/internal/v1/directory/users`, {
    headers: { ...bearer(operatorToken), 'Idempotency-Key': randomUUID() },
    data: {
      issuer: 'https://dev.invalid',
      subject: `vic-${stamp}`,
      display_name: `Vic Social ${stamp}`,
      email: `vic-${stamp}@example.com`,
      role: 'member',
      status: 'active',
    },
  });
  expect(added.status(), await added.text()).toBe(200);
  const vic = inApi('python', 'tools/dev_stack.py', '--dir', '/state', 'token', '--sub', (await added.json()).user_id);
  const before = await (await request.get(`${apiUrl}/v1/whoami`, { headers: bearer(vic) })).json();
  expect([before.role, before.sign_in_mode, before.invite_domains]).toEqual(['member', 'social', ['example.com']]);

  await page.goto('/people');
  await page.getByLabel('API token').fill(token);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Unmatched logins' })).toBeHidden();

  await page.getByRole('button', { name: 'Invite by email' }).click();
  const invite = page.getByRole('dialog', { name: 'Invite by email' });
  await expect(invite.getByText('You can invite addresses at example.com.')).toBeVisible();
  await invite.getByRole('textbox', { name: 'Email' }).fill(`ivy-${stamp}@gmail.com`);
  await invite.getByRole('button', { name: 'Invite', exact: true }).click();
  await expect(invite.getByRole('alert')).toHaveText(
    `ivy-${stamp}@gmail.com is not at example.com. Invite an address at one of these domains.`,
  );
  const outside = await request.post(`${apiUrl}/v1/users/invite`, {
    headers: { ...bearer(token), 'Idempotency-Key': randomUUID() },
    data: { email: `ivy-${stamp}@gmail.com` },
  });
  expect([outside.status(), (await outside.json()).code]).toEqual([422, 'INVITE_DOMAIN_NOT_ALLOWED']);
  await invite.getByRole('textbox', { name: 'Email' }).fill(`ivy-${stamp}@example.com`);
  await invite.getByRole('button', { name: 'Invite', exact: true }).click();
  await expect(page.getByText(`Invited ivy-${stamp}@example.com. They join the first time they sign in.`)).toBeVisible();
  await page.getByRole('group', { name: 'Shown' }).getByText('Invited', { exact: true }).click();
  const roster = page.getByRole('table', { name: 'People, by name' });
  const ivy = roster.getByRole('row').filter({ hasText: `ivy-${stamp}@example.com` });
  await expect(ivy.getByRole('cell').nth(3)).toHaveText('invited');

  await page.getByRole('group', { name: 'Shown' }).getByText('All', { exact: true }).click();
  await page.getByLabel('Search people').fill(`vic social ${stamp}`);
  await roster.getByRole('button', { name: `Vic Social ${stamp}` }).click();
  const person = page.getByRole('dialog', { name: `Vic Social ${stamp}` });
  await person.getByRole('button', { name: 'Deactivate' }).click();
  const confirm = page.getByRole('dialog', { name: `Deactivate Vic Social ${stamp}` });
  await confirm.getByRole('button', { name: 'Deactivate' }).click();
  await expect(person.getByRole('button', { name: 'Reactivate' })).toBeVisible();
  const after = await (await request.get(`${apiUrl}/v1/whoami`, { headers: bearer(vic) })).json();
  expect(after.role).toBeNull();
});
