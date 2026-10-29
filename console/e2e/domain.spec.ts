import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { type APIRequestContext, expect, type Page, test } from '@playwright/test';

const token = process.env.SSC_E2E_TOKEN ?? '';
const composeProject = process.env.SSC_E2E_COMPOSE_PROJECT ?? '';
const composeFile = process.env.SSC_E2E_COMPOSE_FILE ?? '';
const auth = { Authorization: `Bearer ${token}` };
const operatorToken = process.env.SSC_E2E_OPERATOR_TOKEN ?? '';
const apiUrl = process.env.SSC_API_URL ?? '';

function devStack(...args: string[]): string {
  const exec = ['compose', '-p', composeProject, '-f', composeFile, 'exec', '-T', 'api'];
  const command = ['python', 'tools/dev_stack.py', '--dir', '/state', ...args];
  return execFileSync('docker', [...exec, ...command], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] });
}

async function signIn(page: Page, path: string) {
  await page.goto(path);
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel('API token').fill(token);
  await page.getByLabel('Keep it for this tab').check();
  await page.getByRole('button', { name: 'Sign in' }).click();
}

test('an admin delegates a subdomain, moves the apps to it and back', async ({ page, request }) => {
  test.setTimeout(150_000);
  expect(token && composeProject, 'run through e2e/run.mjs').toBeTruthy();
  const stamp = Date.now().toString(36);
  const parent = `e2e${stamp}.example`;
  const suffix = `tools.${parent}`;
  const slug = `e2e-domain-${stamp}`;
  const created = await request.post('/v1/apps', {
    headers: { ...auth, 'Idempotency-Key': randomUUID() },
    data: { slug },
  });
  expect(created.status()).toBe(201);
  devStack('fake-dns', parent, 'ns1.cloudflare.com', 'ns2.cloudflare.com');

  await signIn(page, '/domain');
  await expect(page.getByRole('heading', { name: 'Your own address', level: 1 })).toBeVisible();
  const said = page.getByRole('region', { name: 'Notifications' });
  const form = page.getByRole('form', { name: 'Create your own address' });
  await form.getByLabel('Subdomain to delegate').fill(suffix);
  await form.getByRole('button', { name: 'Create address' }).click();

  await expect(page.getByRole('heading', { name: 'Waiting for your DNS change' })).toBeVisible();
  const records = page.getByRole('table', { name: 'NS records to add' });
  const values = records.getByRole('row').locator('td:nth-child(3) code');
  devStack('domains-tick');
  await page.reload();
  await expect(values.first()).toBeVisible();
  const nameServers = await values.allTextContents();
  expect(nameServers.length).toBeGreaterThan(0);
  await expect(page.getByRole('heading', { name: 'Steps in Cloudflare' })).toBeVisible();
  await expect(page.getByLabel('Ready to paste')).toHaveValue(new RegExp(`DNS zone ${parent.replaceAll('.', '\\.')}`));
  await expect(page.getByText('Nothing yet.')).toBeVisible();

  devStack('fake-dns', suffix, ...nameServers);
  devStack('domains-tick');
  await expect(page.getByRole('heading', { name: 'Issuing certificate' })).toBeVisible({ timeout: 45_000 });

  devStack('fake-certificate', suffix, 'active');
  devStack('domains-tick');
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Active', exact: true })).toBeVisible();
  await expect(page.getByRole('table', { name: 'Apps that move' }).getByText(`${slug}.${suffix}`)).toBeVisible();

  await page.getByRole('button', { name: `Move my apps to ${suffix}` }).click();
  const move = page.getByRole('dialog', { name: `Move my apps to ${suffix}` });
  await move.getByRole('button', { name: 'Move my apps' }).click();
  await expect(said).toContainText(`Your apps now live under ${suffix}.`);
  await expect(page.getByRole('heading', { name: 'Your apps live under your address' })).toBeVisible();
  await expect(page.getByText(/Old addresses under .* redirect until/)).toBeVisible();

  const back = page.getByRole('button', { name: /^Move back to / });
  const defaultSuffix = ((await back.textContent()) ?? '').replace('Move back to ', '');
  await back.click();
  await page.getByRole('dialog', { name: `Move back to ${defaultSuffix}` }).getByRole('button', { name: 'Move back' }).click();
  await expect(said).toContainText(`Your apps now live under ${defaultSuffix}.`);
  await expect(page.getByRole('button', { name: `Move my apps to ${suffix}` })).toBeEnabled();
  await expect(page.getByText(new RegExp(`Old addresses under ${suffix.replaceAll('.', '\\.')} redirect until`))).toBeVisible();
});

async function retireLiveDomain(request: APIRequestContext) {
  const org = (await (await request.get(`${apiUrl}/v1/whoami`, { headers: auth })).json()).org_id;
  const live = (await (await request.get(`${apiUrl}/v1/domain`, { headers: auth })).json()).delegation;
  if (!live || live.state === 'retired') return;
  devStack('fake-dns', live.suffix);
  const retired = await request.post(`${apiUrl}/internal/v1/orgs/${org}/domain/retire`, {
    headers: { Authorization: `Bearer ${operatorToken}`, 'Idempotency-Key': randomUUID() },
  });
  expect(retired.status(), await retired.text()).toBe(200);
}

test('a failed certificate shows the reason and asks again', async ({ page, request }) => {
  test.setTimeout(120_000);
  expect(token && operatorToken && composeProject, 'run through e2e/run.mjs').toBeTruthy();
  await retireLiveDomain(request);
  const parent = `e2ef${Date.now().toString(36)}.example`;
  const suffix = `tools.${parent}`;
  const reason = 'CAA record forbids issuance';
  devStack('fake-dns', parent, 'ns1.cloudflare.com', 'ns2.cloudflare.com');

  await signIn(page, '/domain');
  const form = page.getByRole('form', { name: 'Create your own address' });
  await form.getByLabel('Subdomain to delegate').fill(suffix);
  await form.getByRole('button', { name: 'Create address' }).click();
  await expect(page.getByRole('heading', { name: 'Waiting for your DNS change' })).toBeVisible();
  const values = page.getByRole('table', { name: 'NS records to add' }).getByRole('row').locator('td:nth-child(3) code');
  devStack('domains-tick');
  await page.reload();
  await expect(values.first()).toBeVisible();

  devStack('fake-dns', suffix, ...(await values.allTextContents()));
  devStack('fake-certificate', suffix, 'failed', '--detail', reason);
  devStack('domains-tick');
  devStack('domains-tick');
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Could not set up your address' })).toBeVisible();
  await expect(page.getByText(`${suffix}: ${reason}`)).toBeVisible();

  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.getByRole('region', { name: 'Notifications' })).toContainText(`Asked for ${suffix} again.`);
  await expect(page.getByRole('heading', { name: 'Could not set up your address' })).toBeHidden();
});
