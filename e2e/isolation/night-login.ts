/**
 * The nightly sign-in (SSC-056): `node night-login.ts`. Signs in as the cell's test admin through
 * the real auth host, in a browser, and writes what the night's later steps use.
 *
 *   1. The device flow, as `ssc login` does: the code is approved in the browser, and the tokens
 *      are polled for.
 *   2. A browser sign-in on each app host of `SSC_NIGHT_HOSTS`, which leaves a session at the auth
 *      host and a session cookie on the host.
 *
 * Reads `SSC_NIGHT_AUTH_URL`, `SSC_NIGHT_ORG`, `SSC_NIGHT_USERNAME`, `SSC_NIGHT_PASSWORD`,
 * `SSC_NIGHT_HOSTS` (comma-separated; the last one is the drill host when the drill's file is
 * asked for) and `SSC_NIGHT_OKTA` (`classic` for Okta's classic page). Writes, each with mode 0600:
 *   - `SSC_ISO_AUTH_STATE`: a Playwright storage state holding the auth host's cookies only, and
 *     the user id the tokens name (`user`), which the suite uses as `SSC_ISO_USER` when that is unset;
 *   - `SSC_DRILL_CREDENTIALS_FILE`, when set: the access and refresh tokens and the drill host's
 *     session cookie, as `ssc_conformance.kill_drill` reads them.
 * `SSC_NIGHT_DEBUG_DIR`, when set, is where a failed run writes what it saw (mode 0600): `screenshot.png`,
 * `page.json` (address, visible text, visible fields and buttons; no input value is read) and
 * `steps.log` (which step ran, what was answered and when; never the password).
 * `SSC_NIGHT_BROWSER=webkit` signs in with WebKit instead of Chromium (until f44106e is live, Chromium
 * stops at the device form's Continue). Both files are secrets: never upload them, and delete them when the job ends. `SSC_ISO_PROXY`,
 * set only by the rig, sends the browser through its proxy and accepts its throwaway certificate.
 */
import { chmod, writeFile } from 'node:fs/promises';

import { type BrowserContext, chromium, type Page, webkit } from '@playwright/test';

import { approveDevice, dumpPage, type Login, note, oktaFrom, pollTokens, SESSION_COOKIE, signInOn, startDevice, userOf } from './signin.ts';

const env = process.env;

function required(name: string): string {
  const value = env[name];
  if (!value) throw new Error(`${name} is not set`);
  return value;
}

async function writePrivate(path: string, body: unknown): Promise<void> {
  await writeFile(path, JSON.stringify(body), { mode: 0o600 });
  await chmod(path, 0o600);
}

async function main(): Promise<void> {
  const login: Login = {
    authUrl: required('SSC_NIGHT_AUTH_URL').replace(/\/+$/, ''),
    org: required('SSC_NIGHT_ORG'),
    username: required('SSC_NIGHT_USERNAME'),
    password: required('SSC_NIGHT_PASSWORD'),
    okta: oktaFrom(env.SSC_NIGHT_OKTA),
  };
  const hosts = required('SSC_NIGHT_HOSTS')
    .split(',')
    .map((h) => h.trim())
    .filter((h) => h !== '');
  const statePath = required('SSC_ISO_AUTH_STATE');
  const credentialsPath = env.SSC_DRILL_CREDENTIALS_FILE || null;
  if (hosts.length === 0) throw new Error('SSC_NIGHT_HOSTS names no host');

  const browser = await (env.SSC_NIGHT_BROWSER === 'webkit' ? webkit : chromium).launch();
  try {
    const context = await browser.newContext(env.SSC_ISO_PROXY ? { ignoreHTTPSErrors: true, proxy: { server: env.SSC_ISO_PROXY } } : {});
    const page = await context.newPage();
    try {
      await signIn(page, context, login, hosts, statePath, credentialsPath);
    } catch (error) {
      if (env.SSC_NIGHT_DEBUG_DIR) {
        await dumpPage(page, env.SSC_NIGHT_DEBUG_DIR, error instanceof Error ? error.message : String(error));
        process.stderr.write(`night-login: what the page showed is in ${env.SSC_NIGHT_DEBUG_DIR}\n`);
      }
      throw error;
    }
  } finally {
    await browser.close();
  }
}

async function signIn(
  page: Page,
  context: BrowserContext,
  login: Login,
  hosts: string[],
  statePath: string,
  credentialsPath: string | null,
): Promise<void> {
  const device = await startDevice(context.request, login);
  await approveDevice(page, login, device);
  const tokens = await pollTokens(context.request, login, device);
  note('device flow approved');
  process.stdout.write('device flow approved\n');

  for (const name of hosts) {
    await signInOn(page, login, name);
    process.stdout.write(`signed in on ${name}\n`);
  }

  const authHost = new URL(login.authUrl).host;
  const cookies = (await context.cookies()).filter((c) => c.domain.replace(/^\./, '') === authHost);
  await writePrivate(statePath, { cookies, origins: [], user: userOf(tokens.accessToken) });
  if (credentialsPath !== null) {
    const drillHost = hosts[hosts.length - 1] ?? '';
    const session = (await context.cookies(`https://${drillHost}/`)).find((c) => c.name === SESSION_COOKIE);
    await writePrivate(credentialsPath, {
      auth_url: login.authUrl,
      access_token: tokens.accessToken,
      refresh_token: tokens.refreshToken,
      expires_at: Date.now() / 1000 + tokens.expiresIn,
      cookie: session?.value ?? '',
    });
  }
}

main().catch((error: unknown) => {
  process.stderr.write(`night-login: ${error instanceof Error ? error.message : String(error)}\n`);
  process.exit(1);
});
