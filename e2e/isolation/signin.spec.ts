import { execFile } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';

import { type BrowserContext, expect, test } from '@playwright/test';

import { approveDevice, completeIdp, type Login, oktaFrom, pollTokens, SESSION_COOKIE, signInOn, startDevice, userOf } from './signin';
import { A, B, fast, putSession, SESSION, target, url, whoami } from './support';

const exec = promisify(execFile);
const env = process.env;

test.describe('the nightly sign-in', () => {
  test.skip(fast === false, 'Runs against the rig: its auth host stands in for WorkOS and Okta. The live night runs night-login.ts for real.');

  const login: Login = {
    authUrl: target.authUrl,
    org: env.SSC_NIGHT_ORG ?? '',
    username: env.SSC_NIGHT_USERNAME ?? '',
    password: env.SSC_NIGHT_PASSWORD ?? '',
    okta: oktaFrom(env.SSC_NIGHT_OKTA),
  };

  test('the device code is approved through the identity provider, and its tokens name the person', async ({ page, request }) => {
    const device = await startDevice(page.context().request, login);
    const pending = await request.post(`${login.authUrl}/token`, {
      form: { grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code: device.deviceCode },
    });
    expect([pending.status(), (await pending.json()).error]).toEqual([400, 'authorization_pending']);

    await approveDevice(page, login, device);
    const tokens = await pollTokens(page.context().request, login, device);
    expect(userOf(tokens.accessToken)).toBe(target.user);
    expect(tokens.expiresIn).toBeGreaterThan(0);

    const again = await request.post(`${login.authUrl}/token`, {
      form: { grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code: device.deviceCode },
    });
    expect(again.status(), 'a device code is used up by its tokens').toBe(400);
    const refreshed = await request.post(`${login.authUrl}/token`, { form: { grant_type: 'refresh_token', refresh_token: tokens.refreshToken } });
    expect(refreshed.status()).toBe(200);
    const reused = await request.post(`${login.authUrl}/token`, { form: { grant_type: 'refresh_token', refresh_token: tokens.refreshToken } });
    expect(reused.status(), 'a refresh token rotates').toBe(400);
  });

  test('a wrong password signs in nobody, and the failure never carries it', async ({ page, request }) => {
    const wrong = { ...login, password: 'not-the-password-0123' };
    const device = await startDevice(page.context().request, login);
    await page.goto(device.verificationUrl);
    await page.getByRole('button', { name: 'Continue' }).click();
    const failed = await completeIdp(page, wrong, (address) => address.pathname === '/callback', 4_000).then(
      () => '',
      (error: Error) => error.message,
    );
    expect(failed).toMatch(/did not finish/);
    expect(failed).not.toContain(wrong.password);
    const polled = await request.post(`${login.authUrl}/token`, {
      form: { grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code: device.deviceCode },
    });
    expect((await polled.json()).error, 'nothing was approved').toBe('authorization_pending');
  });

  test('a browser sign-in on an app host meets the form once, and the identity session spares the next', async ({ page, context }) => {
    await signInOn(page, login, A);
    expect((await context.cookies(url(A, '/'))).map((c) => c.name)).toContain(SESSION_COOKIE);
    await context.clearCookies({ name: SESSION_COOKIE });
    const asked: string[] = [];
    page.on('request', (r) => {
      if (new URL(r.url()).pathname === '/idp' && r.method() === 'POST') asked.push(r.url());
    });
    await signInOn(page, login, B);
    expect(asked, 'the second host did not ask again').toEqual([]);
  });

  test("Okta's classic page is answered when SSC_NIGHT_OKTA says classic", async ({ page }) => {
    expect([oktaFrom(undefined), oktaFrom(''), oktaFrom('classic'), oktaFrom('other')]).toEqual(['identity-engine', 'identity-engine', 'classic', 'identity-engine']);
    const classic: Login = { ...login, username: 'night@example.test', password: 'fake-password-1', okta: 'classic' };
    const got: URL[] = [];
    await page.route('https://classic.idp.test/**', async (route) => {
      const address = new URL(route.request().url());
      got.push(address);
      const form =
        '<form action="/done" method="get"><input id="okta-signin-username" name="u"><input id="okta-signin-password" name="p" type="password">' +
        '<input id="okta-signin-submit" type="submit" value="Sign In"></form>';
      await route.fulfill({ contentType: 'text/html', body: address.pathname === '/done' ? '<h1>done</h1>' : form });
    });
    await page.goto('https://classic.idp.test/signin');
    await completeIdp(page, classic, (address) => address.pathname === '/done', 10_000);
    expect(got.at(-1)?.searchParams.get('u')).toBe(classic.username);
    expect(got.at(-1)?.searchParams.get('p')).toBe(classic.password);
  });

  test('night-login.ts writes a state of auth host cookies and a private credentials file', async ({ browser, browserName }, info) => {
    test.skip(browserName !== 'chromium', 'The script drives Chromium itself; once is enough.');
    const dir = mkdtempSync(join(tmpdir(), 'ssc-night-'));
    try {
      const statePath = join(dir, 'state.json');
      const credentialsPath = join(dir, 'credentials.json');
      const run = (extra: Record<string, string>) =>
        exec(process.execPath, ['night-login.ts'], {
          cwd: info.project.testDir,
          env: { ...env, SSC_NIGHT_HOSTS: `${A},${B}`, SSC_ISO_AUTH_STATE: statePath, SSC_DRILL_CREDENTIALS_FILE: credentialsPath, ...extra },
        });

      const missing = await run({ SSC_NIGHT_PASSWORD: '' }).then(
        () => null,
        (error: { code: number; stderr: string }) => error,
      );
      expect([missing?.code, missing?.stderr]).toEqual([1, 'night-login: SSC_NIGHT_PASSWORD is not set\n']);

      const out = await run({});
      const state = JSON.parse(readFileSync(statePath, 'utf8')) as { cookies: Parameters<BrowserContext['addCookies']>[0]; user: string };
      const credentials = JSON.parse(readFileSync(credentialsPath, 'utf8')) as Record<string, string | number>;
      for (const path of [statePath, credentialsPath]) {
        expect(statSync(path).mode & 0o777, path).toBe(0o600);
        expect(readFileSync(path, 'utf8'), 'the password is written nowhere').not.toContain(login.password);
      }
      expect(out.stdout + out.stderr).not.toContain(login.password);
      expect(state.user).toBe(target.user);
      expect(state.cookies.length).toBeGreaterThan(0);
      expect(new Set(state.cookies.map((c) => (c.domain ?? '').replace(/^\./, ''))), 'the auth host only').toEqual(new Set([new URL(login.authUrl).host]));
      expect(credentials.auth_url).toBe(login.authUrl);
      expect(Number(credentials.expires_at)).toBeGreaterThan(Date.now() / 1000);
      expect(credentials.access_token).toBeTruthy();
      expect(credentials.refresh_token).toBeTruthy();

      const context = await browser.newContext({ ignoreHTTPSErrors: true, ...(target.proxy ? { proxy: { server: target.proxy } } : {}) });
      const page = await context.newPage();
      await putSession(context, B, String(credentials.cookie));
      expect((await whoami(page, B)).sub, "the drill host's cookie is the person's session there").toBe(target.user);
      expect((await context.cookies(url(B, '/'))).some((c) => c.name === SESSION)).toBe(true);

      const fresh = await browser.newContext({ ignoreHTTPSErrors: true, ...(target.proxy ? { proxy: { server: target.proxy } } : {}) });
      await fresh.addCookies(state.cookies);
      const other = await fresh.newPage();
      expect((await other.goto(url(A, '/')))?.status(), 'the recorded auth host session signs in on a new host').toBe(200);
      expect(new URL(other.url()).host).toBe(A);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
