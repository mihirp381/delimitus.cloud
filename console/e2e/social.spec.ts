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

function setSignIn(org: string, mode: 'social' | 'directory'): void {
  const providers = mode === 'social' ? ' --providers google,github' : '';
  const cli = `python -m ssc_control.identity set-sign-in --org ${org} --mode ${mode}${providers} --operator op_e2e`;
  inApi('sh', '-c', `SSC_DATABASE_DSN=${APP_DSN} ${cli}`);
}

let org = '';

test.beforeAll(async ({ request }) => {
  expect(token && operatorToken && composeProject, 'run through e2e/run.mjs').toBeTruthy();
  org = (await (await request.get(`${apiUrl}/v1/whoami`, { headers: bearer(token) })).json()).org_id;
  setSignIn(org, 'social');
});

test.afterAll(() => {
  if (org) setSignIn(org, 'directory');
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
  expect([before.role, before.sign_in_mode]).toEqual(['member', 'social']);

  await page.goto('/people');
  await page.getByLabel('API token').fill(token);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Unmatched logins' })).toBeHidden();

  await page.getByRole('button', { name: 'Invite by email' }).click();
  const invite = page.getByRole('dialog', { name: 'Invite by email' });
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
