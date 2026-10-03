/**
 * The target the suite runs against and the helpers every case shares (SSC-029). Everything comes
 * from `SSC_ISO_*` in the environment, which `run.mjs` sets: for `fast` from the rig's
 * announcement, for `live` from the operator's settings (README). Nothing here names a real host.
 */
import { type Browser, type BrowserContext, expect, type Page, type Response } from '@playwright/test';

const env = process.env;

export const target = {
  name: env.SSC_ISO_TARGET === 'live' ? 'live' : 'fast',
  base: env.SSC_ISO_CELL1_BASE ?? '',
  cell2: env.SSC_ISO_CELL2_BASE || null,
  authUrl: env.SSC_ISO_AUTH_URL ?? '',
  control: env.SSC_ISO_CONTROL || null,
  proxy: env.SSC_ISO_PROXY || null,
  limitSeconds: Number(env.SSC_ISO_LIMIT_SECONDS || '3600'),
  user: env.SSC_ISO_USER ?? '',
  otherUser: env.SSC_ISO_OTHER_USER ?? '',
  outsider: env.SSC_ISO_OUTSIDER ?? '',
  authState: env.SSC_ISO_AUTH_STATE || null,
  gatewayRunApp: env.SSC_ISO_GATEWAY_RUN_APP || null,
  nightly: env.SSC_ISO_NIGHTLY === '1',
};

export const fast = target.name === 'fast';
/** The browser's proxy for the target's hosts: the rig's, which resolves them; none live. */
export const proxy = target.proxy ? { proxy: { server: target.proxy } } : {};
export const SESSION = '__Host-ssc-session';
export const WAKE = '__Host-ssc-wake';
export const PERSON = 'iso-person';

/** Sessions can be sealed: the rig, or the live sealer with the cell's keyring. */
export const canSeal = target.control !== null && target.user !== '';
/** A real sign-in through the auth host: the rig's stand-in, or a recorded auth host session. */
export const canLogin = fast || target.authState !== null;
export const NO_SESSIONS = 'No sealed sessions: set SSC_ISO_CELL1_KEYRING, SSC_ISO_CELL1_ORG and the people (README).';
export const NO_LOGIN = 'Needs a cookie the gateway set at sign-in: the auth host is not deployed (SSC-064), or SSC_ISO_AUTH_STATE is not set.';

export function host(app: string, base = target.base): string {
  return `${app}.${base}`;
}

export const A = host('alpha');
export const B = host('bravo');
export const LIMITED = host('bravo--preview');

export function url(name: string, path = '/'): string {
  return `https://${name}${path}`;
}

/** A mark no other run shares, to find this case's requests in an app's log. */
export function mark(): string {
  return `m${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

export async function seal(name: string, user: string): Promise<string> {
  const response = await fetch(`${target.control}/seal`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ host: name, user }),
  });
  expect(response.status, 'the sealer answers').toBe(200);
  return ((await response.json()) as { value: string }).value;
}

export async function putSession(context: BrowserContext, name: string, value: string): Promise<void> {
  await context.addCookies([{ name: SESSION, value, url: url(name), secure: true, httpOnly: true, sameSite: 'Lax' }]);
}

/** A session for `user` on host `name`, sealed rather than signed in (SSC-086). */
export async function useSession(context: BrowserContext, name: string, user: string): Promise<void> {
  await putSession(context, name, await seal(name, user));
}

/** A second browser, as another person or a thief would have; it shares nothing with the first. */
export async function otherBrowser(browser: Browser): Promise<Page> {
  const context = await browser.newContext({ ignoreHTTPSErrors: fast, ...proxy });
  return context.newPage();
}

/** Lets `who` through the auth host: the rig's stand-in signs in whoever its cookie names; live,
 * the recorded auth host session (SSC_ISO_AUTH_STATE) is for SSC_ISO_USER only. */
export async function allowLogin(context: BrowserContext, who = target.user): Promise<void> {
  if (fast) {
    await context.addCookies([{ name: PERSON, value: who, url: target.authUrl, secure: true, sameSite: 'Lax' }]);
    return;
  }
  expect(who, 'the recorded auth host session is for SSC_ISO_USER').toBe(target.user);
  const { readFile } = await import('node:fs/promises');
  const state = JSON.parse(await readFile(target.authState ?? '', 'utf8')) as { cookies: Parameters<BrowserContext['addCookies']>[0] };
  await context.addCookies(state.cookies);
}

/** Sign in on `name` through the auth host and return the hand-back address the browser used. */
export async function signIn(page: Page, name: string, who = target.user): Promise<string> {
  await allowLogin(page.context(), who);
  let callback = '';
  const watch = (request: { url(): string }) => {
    if (request.url().startsWith(url(name, '/.ssc/callback?'))) callback = request.url();
  };
  page.on('request', watch);
  const response = await page.goto(url(name, '/'));
  page.off('request', watch);
  expect(new URL(page.url()).host, 'back on the app after signing in').toBe(name);
  expect(response?.status()).toBe(200);
  expect(callback, 'the sign-in went through the hand-back').not.toBe('');
  return callback;
}

/** The response to `address` while `go` runs. Not for a redirect: WebKit does not report one across
 * a cross-site navigation, which is why `sentToLogin` watches requests instead. */
export async function answerTo(page: Page, address: string, go: () => Promise<unknown>): Promise<Response> {
  const answer = page.waitForResponse((r) => r.url() === address);
  await go();
  return answer;
}

/** Run `go` (by default, open `address`) and expect the gateway to send the browser to sign in:
 * the browser next asks the auth host's `/login` to come back to `address`, which it does only on
 * the gateway's redirect. This watches requests, not the redirect itself, which WebKit does not
 * report across a cross-site navigation. */
export async function sentToLogin(page: Page, address: string, go: () => Promise<unknown> = () => page.goto(address)): Promise<URL> {
  const login = page.waitForRequest(
    (r) => {
      const u = new URL(r.url());
      return `${u.origin}${u.pathname}` === `${target.authUrl}/login` && u.searchParams.get('return_to') === address;
    },
    { timeout: 15_000 },
  );
  await go();
  return new URL((await login).url());
}

/** Start signing in on `name` and hold the hand-back, so the code goes to nobody yet: the browser
 * gets a login nonce from the app, then the auth host's answer is read without following it. */
export async function holdCode(page: Page, name: string): Promise<string> {
  const login = await sentToLogin(page, url(name, '/'));
  await allowLogin(page.context());
  const answer = await page.request.get(login.href, { maxRedirects: 0 });
  const held = answer.headers()['location'] ?? '';
  expect(held.startsWith(url(name, '/.ssc/callback?')) && new URL(held).searchParams.has('code'), 'the auth host handed back a code').toBe(true);
  return held;
}

/** Have `page` start signing in on `name`, so it holds a login nonce of its own there. */
export async function startLogin(page: Page, name: string): Promise<Page> {
  await sentToLogin(page, url(name, '/'));
  return page;
}

export type Seen = { app: string | null; sub: string | null; cookie: string | null; deadline: string | null };

/** What app `name` was told about a request from this browser (its `/whoami`), from its own page. */
export async function whoami(page: Page, name: string): Promise<Seen> {
  await page.goto(url(name, '/'));
  return page.evaluate(async () => (await fetch('/whoami')).json());
}

/** The marks of the requests that reached app `name` and start with `prefix`. */
export async function reached(page: Page, name: string, prefix: string): Promise<string[]> {
  await page.goto(url(name, '/'));
  const log: { kind: string; m: string | null }[] = await page.evaluate(async () => (await fetch('/log')).json());
  return log.filter((e) => e.m?.startsWith(prefix)).map((e) => `${e.kind}:${e.m}`);
}

/** Every `Set-Cookie` value of a response, one per entry. */
export async function setCookies(response: Response): Promise<string[]> {
  return (await response.headersArray())
    .filter((h) => h.name.toLowerCase() === 'set-cookie')
    .flatMap((h) => h.value.split('\n'))
    .filter((v) => v !== '');
}

export async function control(name: 'authoriser' | 'snapshot', on: boolean): Promise<void> {
  const response = await fetch(`${target.control}/${name}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ on }),
  });
  expect(response.status).toBe(204);
}
