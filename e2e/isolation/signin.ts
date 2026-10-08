/**
 * Signing in to a cell the way a person does, for the nightly run (SSC-056): the real auth host,
 * through WorkOS to the org's Okta, as the cell's test admin. Used by `night-login.ts` (which
 * writes what the night needs) and by `signin.spec.ts` (which runs it against the rig's stand-in).
 * Nothing here reads the environment, names a host or logs the password.
 */
import { chmod, mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

import type { APIRequestContext, Page } from '@playwright/test';

export const SESSION_COOKIE = '__Host-ssc-session';
const DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code';
const STEP_MS = 250;
const TRIES = 3;
export const SIGN_IN_TIMEOUT_MS = 90_000;
const SLOW_DOWN_SECONDS = 5;
/** How long the identity provider may show nothing the loop answers, after the password, before the run says what it shows. */
export const STALL_MS = 20_000;
/** A form that was just answered is left alone this long: Okta may keep its button enabled while it works, and a second submit of a password could lock the account. */
const SETTLE_MS = 8_000;

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
    error: '.o-form-error-container, .okta-form-infobox-error',
  },
  classic: {
    username: '#okta-signin-username',
    password: '#okta-signin-password',
    submit: '#okta-signin-submit',
    chooser: '',
    methods: '',
    error: '.o-form-error-container, .okta-form-infobox-error',
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


const startedAt = Date.now();
const steps: string[] = [];

/** What the run did and when, for the failure's dump: never a value the person typed. */
export function note(message: string): void {
  steps.push(`+${((Date.now() - startedAt) / 1000).toFixed(1)}s ${message}`);
}

/** Where a page is, without its query: addresses can carry codes. */
function where(address: string): string {
  try {
    const url = new URL(address);
    const keys = [...url.searchParams.keys()];
    return `${url.host}${url.pathname}${keys.length > 0 ? ` (query: ${keys.join(',')})` : ''}`;
  } catch {
    return address;
  }
}

/** What the page says on the screen, short, for a failure's message. Reads no input value. */
async function said(page: Page): Promise<string> {
  const text = await page
    .evaluate(() => {
      const shown = (e: Element) => (e as HTMLElement).offsetParent !== null;
      const heads = [...document.querySelectorAll('h1,h2,h3,.okta-form-title,.o-form-error-container,.okta-form-infobox-error')]
        .filter(shown)
        .map((e) => (e as HTMLElement).innerText.trim())
        .filter((t) => t !== '');
      return (heads.length > 0 ? heads.join(' | ') : document.body.innerText).replace(/\s+/g, ' ').slice(0, 300);
    })
    .catch(() => '');
  return text === '' ? 'nothing readable' : text;
}

/**
 * Write what a failed run saw into `dir` (mode 0600): `screenshot.png`, `page.json` (address
 * without its query values, the visible text, the visible fields and buttons) and `steps.log`.
 * No input value is read, so no password reaches a file; a screenshot shows dots. Never throws.
 */
export async function dumpPage(page: Page, dir: string, failure: string): Promise<void> {
  try {
    await mkdir(dir, { recursive: true, mode: 0o700 });
    const write = async (name: string, body: string | Buffer) => {
      await writeFile(join(dir, name), body, { mode: 0o600 });
      await chmod(join(dir, name), 0o600);
    };
    await write('steps.log', `${steps.join('\n')}\nfailure: ${failure}\n`);
    const seen = await page
      .evaluate(() => {
        const shown = (e: Element) => (e as HTMLElement).offsetParent !== null;
        const attrs = (e: Element) => {
          const out: Record<string, string> = { tag: e.tagName.toLowerCase() };
          for (const name of ['type', 'name', 'id', 'class', 'data-se', 'role', 'href', 'aria-disabled']) {
            const value = e.getAttribute(name);
            if (value !== null) out[name] = name === 'href' ? value.split('?')[0] ?? '' : value;
          }
          const type = (e.getAttribute('type') ?? '').toLowerCase();
          if (e.tagName === 'INPUT' && (type === 'submit' || type === 'button')) out.value = (e as HTMLInputElement).value;
          if ('disabled' in e) out.disabled = String((e as HTMLButtonElement).disabled);
          const label = (e as HTMLElement).innerText;
          if (e.tagName !== 'INPUT' && label) out.text = label.trim().slice(0, 120);
          return out;
        };
        return {
          text: document.body ? document.body.innerText : '',
          fields: [...document.querySelectorAll('input,button,select,textarea,a,form,[role=button]')].filter(shown).map(attrs),
          frames: window.frames.length,
        };
      })
      .catch((error: unknown) => ({ text: '', fields: [], frames: 0, unreadable: String(error) }));
    await write('page.json', JSON.stringify({ at: new Date().toISOString(), address: where(page.url()), ...seen }, null, 2));
    const shot = await page.screenshot({ fullPage: true, timeout: 10_000 }).catch(() => null);
    if (shot !== null) await write('screenshot.png', shot);
  } catch {
    // a dump that cannot be written must not hide the failure it describes
  }
}

/**
 * Answer the identity provider's sign-in form wherever the browser meets it until `done` is true
 * of the page's address. A person who is already signed in to Okta meets no form, and the loop
 * ends at once. A form that comes back after it was answered three times is a failure, named by
 * the field and never by what was typed.
 */
export async function completeIdp(
  page: Page,
  login: Login,
  done: (address: URL) => boolean,
  timeout = SIGN_IN_TIMEOUT_MS,
  label = 'sign-in',
  stall = STALL_MS,
): Promise<void> {
  const fields = OKTA[login.okta];
  const answered = { username: 0, password: 0 };
  const answeredAt = { username: 0, password: 0 };
  const deadline = Date.now() + timeout;
  let chosen = 0;
  let last = '';
  let quietSince = Date.now();
  note(`${label}: at ${where(page.url())}`);
  while (Date.now() < deadline) {
    const address = new URL(page.url());
    if (`${address.host}${address.pathname}` !== last) {
      last = `${address.host}${address.pathname}`;
      quietSince = Date.now();
      if (answered.username + answered.password + chosen > 0) note(`${label}: moved to ${where(page.url())}`);
    }
    if (done(address)) {
      note(`${label}: done at ${where(page.url())}`);
      return;
    }
    if (answered.username + answered.password > 0) {
      // Okta says why it refused an answer; retyping a refused password could lock the account.
      const why = await page
        .locator(fields.error)
        .first()
        .innerText({ timeout: 1_000 })
        .catch(() => '');
      if (why.trim() !== '') throw new Error(`the identity provider refused the ${answered.password > 0 ? 'password' : 'username'}: ${why.trim().replace(/\s+/g, ' ').slice(0, 200)}`);
    }
    if (fields.methods && (await shown(page, fields.methods))) {
      if (answered.password > 0 || !(await shown(page, fields.chooser))) throw new Error(PASSWORD_ONLY);
      if (++chosen > TRIES) throw new Error(`the identity provider offered its security methods ${TRIES} times`);
      note(`${label}: chose the password method`);
      quietSince = Date.now();
      await page.locator(fields.chooser).first().click({ timeout: 5_000 }).catch(() => undefined);
      await sleep(STEP_MS);
      continue;
    }
    const [asksName, asksPassword] = [await shown(page, fields.username), await shown(page, fields.password)];
    const submit = page.locator(fields.submit).first();
    // Okta disables its button while it handles an answer; the old field is still on the page then.
    if ((!asksName && !asksPassword) || !(await submit.isEnabled().catch(() => false))) {
      // After the password, a page with no form to answer is Okta asking for something else (a push,
      // a code, an enrolment): say what it shows rather than wait out the clock.
      if (answered.password > 0 && !asksName && !asksPassword && Date.now() - quietSince > stall) {
        throw new Error(`after the password the identity provider shows a page the script cannot answer: ${await said(page)}`);
      }
      await sleep(STEP_MS);
      continue;
    }
    if ((asksName && Date.now() - answeredAt.username < SETTLE_MS) || (asksPassword && Date.now() - answeredAt.password < SETTLE_MS)) {
      await sleep(STEP_MS);
      continue;
    }
    for (const [asked, field, value] of [
      [asksName, 'username', login.username],
      [asksPassword, 'password', login.password],
    ] as const) {
      if (!asked) continue;
      if (++answered[field] > TRIES) throw new Error(`the identity provider asked for the ${field} ${TRIES} times`);
      answeredAt[field] = Date.now();
      note(`${label}: answered the ${field} (time ${answered[field]})`);
      await type(page, fields[field], field, value);
    }
    quietSince = Date.now();
    await Promise.all([page.waitForLoadState('load').catch(() => undefined), submit.click({ timeout: 5_000 }).catch(() => undefined)]);
    await sleep(STEP_MS);
  }
  throw new Error(`the sign-in did not finish within ${Math.round(timeout / 1000)} seconds, at ${new URL(page.url()).host}`);
}

/** Approve a device grant as the person: the form the host shows, then the identity provider. */
export async function approveDevice(page: Page, login: Login, device: Device): Promise<void> {
  note('device approval: opening the verification page');
  await page.goto(device.verificationUrl);
  await page.getByRole('button', { name: 'Continue' }).click();
  const auth = new URL(login.authUrl);
  await completeIdp(page, login, (address) => address.host === auth.host && address.pathname === '/callback', SIGN_IN_TIMEOUT_MS, 'device approval');
  await page.getByText('You are signed in').waitFor({ timeout: 15_000 });
}

/** Sign in on the app host `name` through the auth host; fails unless its session cookie is set. */
export async function signInOn(page: Page, login: Login, name: string): Promise<void> {
  note(`sign-in on ${name}: opening it`);
  await page.goto(`https://${name}/`).catch(() => undefined);
  await completeIdp(page, login, (address) => address.host === name && address.pathname !== '/.ssc/callback', SIGN_IN_TIMEOUT_MS, `sign-in on ${name}`);
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
