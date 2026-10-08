/**
 * Signing in to a cell the way a person does, for the nightly run (SSC-056): the real auth host,
 * through WorkOS to the org's Okta, as the cell's test admin. Used by `night-login.ts` (which
 * writes what the night needs) and by `signin.spec.ts` (which runs it against the rig's stand-in).
 * Nothing here reads the environment, names a host or logs the password.
 */
import type { APIRequestContext, Page } from '@playwright/test';

export const SESSION_COOKIE = '__Host-ssc-session';
const DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code';
const STEP_MS = 250;
const TRIES = 3;
export const SIGN_IN_TIMEOUT_MS = 90_000;
const SLOW_DOWN_SECONDS = 5;

export type Login = {
  authUrl: string;
  org: string;
  username: string;
  password: string;
  /** Okta's sign-in page: `identity-engine` (the default) or the `classic` one. */
  okta: 'identity-engine' | 'classic';
};

/** The fields of Okta's sign-in forms. Identity Engine asks for the name, then, when the person has
 * more than one security method, which one (`chooser` picks Password; live on org 2's Okta,
 * 2026-10-08), then the password; the classic page has both on one form. `methods` is the list of
 * security methods: met once the password is answered, it means Okta wants a second one. */
export const OKTA = {
  'identity-engine': {
    username: 'input[name="identifier"]',
    password: 'input[name="credentials.passcode"]',
    submit: 'input[type="submit"]',
    chooser: '[data-se="okta_password"] [data-se="button"]',
    methods: '.authenticator-verify-list',
  },
  classic: {
    username: '#okta-signin-username',
    password: '#okta-signin-password',
    submit: '#okta-signin-submit',
    chooser: '',
    methods: '',
  },
} as const;

const PASSWORD_ONLY = 'the identity provider wants a security method other than the password; the test user must sign in with a password alone';

export type Device = {
  deviceCode: string;
  userCode: string;
  verificationUrl: string;
  expiresIn: number;
  interval: number;
};

export type Tokens = { accessToken: string; refreshToken: string; expiresIn: number };

export function oktaFrom(value: string | undefined): Login['okta'] {
  return value === 'classic' ? 'classic' : 'identity-engine';
}

async function json(response: Awaited<ReturnType<APIRequestContext['post']>>): Promise<Record<string, unknown>> {
  return (await response.json().catch(() => ({}))) as Record<string, unknown>;
}

/** Start the command line's device flow for the org (`POST /device/authorize`). */
export async function startDevice(request: APIRequestContext, login: Login): Promise<Device> {
  const response = await request.post(`${login.authUrl}/device/authorize`, { form: { org: login.org } });
  const body = await json(response);
  if (response.status() !== 200 || typeof body.device_code !== 'string' || typeof body.verification_uri_complete !== 'string') {
    throw new Error(`the auth host refused the device flow (HTTP ${response.status()})`);
  }
  return {
    deviceCode: body.device_code,
    userCode: String(body.user_code),
    verificationUrl: body.verification_uri_complete,
    expiresIn: Number(body.expires_in),
    interval: Number(body.interval),
  };
}

/** Wait for the tokens of an approved device grant: poll at the interval the host named. */
export async function pollTokens(request: APIRequestContext, login: Login, device: Device, wait = sleep): Promise<Tokens> {
  let every = Math.max(1, device.interval);
  const deadline = Date.now() + device.expiresIn * 1000;
  while (Date.now() < deadline) {
    const response = await request.post(`${login.authUrl}/token`, { form: { grant_type: DEVICE_GRANT, device_code: device.deviceCode } });
    const body = await json(response);
    if (response.status() === 200) {
      return {
        accessToken: String(body.access_token),
        refreshToken: String(body.refresh_token),
        expiresIn: Number(body.expires_in),
      };
    }
    if (body.error === 'slow_down') every += SLOW_DOWN_SECONDS;
    else if (body.error !== 'authorization_pending') throw new Error(`the device grant ended: ${String(body.error)}`);
    await wait(every * 1000);
  }
  throw new Error('the device code expired before it was approved');
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Fill a field; a failure names the field, as Playwright's own message would carry the value. */
async function type(page: Page, selector: string, field: string, value: string): Promise<void> {
  await page
    .locator(selector)
    .first()
    .fill(value)
    .catch(() => {
      throw new Error(`could not fill the ${field} field`);
    });
}

async function shown(page: Page, selector: string): Promise<boolean> {
  return page
    .locator(selector)
    .first()
    .isVisible()
    .catch(() => false);
}

/**
 * Answer the identity provider's sign-in form wherever the browser meets it until `done` is true
 * of the page's address. A person who is already signed in to Okta meets no form, and the loop
 * ends at once. A form that comes back after it was answered three times is a failure, named by
 * the field and never by what was typed.
 */
export async function completeIdp(page: Page, login: Login, done: (address: URL) => boolean, timeout = SIGN_IN_TIMEOUT_MS): Promise<void> {
  const fields = OKTA[login.okta];
  const answered = { username: 0, password: 0 };
  const deadline = Date.now() + timeout;
  let chosen = 0;
  while (Date.now() < deadline) {
    if (done(new URL(page.url()))) return;
    if (fields.methods && (await shown(page, fields.methods))) {
      if (answered.password > 0 || !(await shown(page, fields.chooser))) throw new Error(PASSWORD_ONLY);
      if (++chosen > TRIES) throw new Error(`the identity provider offered its security methods ${TRIES} times`);
      await page.locator(fields.chooser).first().click({ timeout: 5_000 }).catch(() => undefined);
      await sleep(STEP_MS);
      continue;
    }
    const [asksName, asksPassword] = [await shown(page, fields.username), await shown(page, fields.password)];
    const submit = page.locator(fields.submit).first();
    // Okta disables its button while it handles an answer; the old field is still on the page then.
    if ((!asksName && !asksPassword) || !(await submit.isEnabled().catch(() => false))) {
      await sleep(STEP_MS);
      continue;
    }
    for (const [asked, field, value] of [
      [asksName, 'username', login.username],
      [asksPassword, 'password', login.password],
    ] as const) {
      if (!asked) continue;
      if (++answered[field] > TRIES) throw new Error(`the identity provider asked for the ${field} ${TRIES} times`);
      await type(page, fields[field], field, value);
    }
    await Promise.all([page.waitForLoadState('load').catch(() => undefined), submit.click({ timeout: 5_000 }).catch(() => undefined)]);
    await sleep(STEP_MS);
  }
  throw new Error(`the sign-in did not finish within ${Math.round(timeout / 1000)} seconds, at ${new URL(page.url()).host}`);
}

/** Approve a device grant as the person: the form the host shows, then the identity provider. */
export async function approveDevice(page: Page, login: Login, device: Device): Promise<void> {
  await page.goto(device.verificationUrl);
  await page.getByRole('button', { name: 'Continue' }).click();
  const auth = new URL(login.authUrl);
  await completeIdp(page, login, (address) => address.host === auth.host && address.pathname === '/callback');
  await page.getByText('You are signed in').waitFor({ timeout: 15_000 });
}

/** Sign in on the app host `name` through the auth host; fails unless its session cookie is set. */
export async function signInOn(page: Page, login: Login, name: string): Promise<void> {
  await page.goto(`https://${name}/`).catch(() => undefined);
  await completeIdp(page, login, (address) => address.host === name && address.pathname !== '/.ssc/callback');
  const cookies = await page.context().cookies(`https://${name}/`);
  if (!cookies.some((c) => c.name === SESSION_COOKIE)) throw new Error(`no session on ${name} after signing in`);
}

/** The user id an access token names (`sub`). The token is read, not verified: the auth host that
 * issued it just answered over TLS. */
export function userOf(accessToken: string): string {
  const payload = accessToken.split('.')[1] ?? '';
  try {
    const sub = (JSON.parse(Buffer.from(payload, 'base64url').toString('utf8')) as { sub?: unknown }).sub;
    if (typeof sub === 'string' && sub !== '') return sub;
  } catch {
    // falls through to the error: a token that is not a JWT has no user to name
  }
  throw new Error('the access token does not name a user');
}
