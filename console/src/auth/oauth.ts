/**
 * Production sign-in: the console is the auth host's first-party OAuth client `ssc-console`
 * (decision 029). Authorization code with S256 PKCE; the verifier, state and return path wait in
 * sessionStorage for the one callback and are removed before it is checked. The answer must carry
 * the same state and this auth host as `iss` (RFC 9207). Tokens never touch storage: see
 * `session.ts`.
 */

export const CLIENT_ID = 'ssc-console';
export const CALLBACK_PATH = '/auth/callback';
/** The auth host's origin, which is also the `iss` it answers with. */
export const AUTH_URL: string = import.meta.env.VITE_SSC_AUTH_URL || 'https://auth.delimitus.com';
/** The resource the console asks for: the API's user audience. */
export const API_AUDIENCE: string =
  import.meta.env.VITE_SSC_API_AUDIENCE || 'https://api.delimitus.com';
/** Where a sign-in in progress waits for its callback. */
export const PENDING_KEY = 'ssc.console.oauth';

export interface OAuthConfig {
  readonly authUrl: string;
  readonly audience: string;
  /** `<console origin>/auth/callback`, exactly as the auth host knows it. */
  readonly redirectUri: string;
  readonly storage: Storage;
  readonly fetch?: (input: string, init: RequestInit) => Promise<Response>;
  /** Leaves the console for the auth host; `window.location.assign` unless a test replaces it. */
  readonly go?: (url: string) => void;
}

export interface TokenSet {
  readonly accessToken: string;
  readonly refreshToken: string;
  /** Seconds the access token lives, from the auth host's `expires_in`. */
  readonly expiresIn: number;
}

/** What the session needs from the auth host to keep a sign-in alive and to end it. */
export interface Refresher {
  refresh(refreshToken: string): Promise<TokenSet>;
  revoke(refreshToken: string): Promise<void>;
}

export interface OAuthClient extends Refresher {
  /** Sends the browser to the auth host; `next` is the same-site path to come back to. */
  signIn(next?: string): Promise<void>;
  /** Checks the callback's query and exchanges its code; throws `SignInError` on any mismatch. */
  complete(search: string): Promise<{ tokens: TokenSet; next: string }>;
}

/** A sign-in that did not finish; the message is safe to show. */
export class SignInError extends Error {
  override name = 'SignInError';
}

/** What to show for a sign-in that failed: its own message, or that the auth host was not reached. */
export function signInMessage(error: unknown): string {
  return error instanceof SignInError ? error.message : 'The SSC auth host could not be reached.';
}

/** Only same-site paths are followed after sign-in. */
export function safeNext(value: unknown): string | undefined {
  return typeof value === 'string' &&
    value.startsWith('/') &&
    !value.startsWith('//') &&
    !value.includes('\\')
    ? value
    : undefined;
}

interface Pending {
  readonly verifier: string;
  readonly state: string;
  readonly next: string;
}

export function base64url(bytes: Uint8Array): string {
  let text = '';
  for (const b of bytes) text += String.fromCharCode(b);
  return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function randomString(bytes: number): string {
  return base64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

/** RFC 7636 S256: base64url(SHA-256(verifier)). */
export async function challengeFor(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

/** A 43-character verifier (32 random bytes) and its S256 challenge. */
export async function newPkce(): Promise<{ verifier: string; challenge: string }> {
  const verifier = randomString(32);
  return { verifier, challenge: await challengeFor(verifier) };
}

/** The pending sign-in, removed from storage whatever it holds: it is good for one answer. */
function takePending(storage: Storage): Pending | null {
  try {
    const raw = storage.getItem(PENDING_KEY);
    storage.removeItem(PENDING_KEY);
    const value = (raw ? JSON.parse(raw) : null) as Partial<Pending> | null;
    if (
      value &&
      typeof value.verifier === 'string' &&
      typeof value.state === 'string' &&
      typeof value.next === 'string'
    ) {
      return { verifier: value.verifier, state: value.state, next: value.next };
    }
  } catch {
    // Blocked storage or a damaged entry: no sign-in is pending.
  }
  return null;
}

function tokenSet(body: unknown): TokenSet {
  const b = (body ?? {}) as Record<string, unknown>;
  if (
    typeof b.access_token !== 'string' ||
    typeof b.refresh_token !== 'string' ||
    typeof b.expires_in !== 'number' ||
    typeof b.token_type !== 'string' ||
    b.token_type.toLowerCase() !== 'bearer'
  ) {
    throw new SignInError('The auth host sent an answer the console does not understand.');
  }
  return { accessToken: b.access_token, refreshToken: b.refresh_token, expiresIn: b.expires_in };
}

export function createOAuth(config: OAuthConfig): OAuthClient {
  const send = config.fetch ?? ((input, init) => globalThis.fetch(input, init));
  const go = config.go ?? ((url: string) => window.location.assign(url));

  // A form body makes a CORS "simple request": no preflight, and never cookies or credentials.
  function post(path: '/token' | '/revoke', form: Record<string, string>): Promise<Response> {
    return send(`${config.authUrl}${path}`, {
      method: 'POST',
      body: new URLSearchParams(form),
      credentials: 'omit',
      cache: 'no-store',
    });
  }

  async function grant(form: Record<string, string>): Promise<TokenSet> {
    const response = await post('/token', form);
    if (!response.ok) throw new SignInError('The auth host refused the sign-in.');
    return tokenSet(await response.json());
  }

  return {
    async signIn(next = '/') {
      const { verifier, challenge } = await newPkce();
      const state = randomString(32);
      const pending: Pending = { verifier, state, next };
      config.storage.setItem(PENDING_KEY, JSON.stringify(pending));
      const query = new URLSearchParams({
        response_type: 'code',
        client_id: CLIENT_ID,
        redirect_uri: config.redirectUri,
        state,
        code_challenge: challenge,
        code_challenge_method: 'S256',
        resource: config.audience,
      });
      go(`${config.authUrl}/authorize?${query.toString()}`);
    },

    async complete(search) {
      const pending = takePending(config.storage);
      const q = new URLSearchParams(search);
      if (pending === null) throw new SignInError('No sign-in was started in this tab.');
      if (q.get('state') !== pending.state) {
        throw new SignInError('This answer is not for the sign-in started in this tab.');
      }
      if (q.get('iss') !== config.authUrl) {
        throw new SignInError('This answer did not come from the SSC auth host.');
      }
      const error = q.get('error');
      if (error === 'access_denied') throw new SignInError('Sign-in was cancelled.');
      if (error) throw new SignInError(`Sign-in did not finish (${error}).`);
      const code = q.get('code');
      if (!code) throw new SignInError('The answer has no code.');
      const tokens = await grant({
        grant_type: 'authorization_code',
        client_id: CLIENT_ID,
        code,
        redirect_uri: config.redirectUri,
        code_verifier: pending.verifier,
        resource: config.audience,
      });
      return { tokens, next: pending.next };
    },

    refresh(refreshToken) {
      return grant({ grant_type: 'refresh_token', refresh_token: refreshToken });
    },

    async revoke(refreshToken) {
      await post('/revoke', { token: refreshToken, token_type_hint: 'refresh_token' });
    },
  };
}

/** The console's own client: this build's auth host and audience, the tab's sessionStorage. */
export function consoleOAuth(origin: string): OAuthClient {
  return createOAuth({
    authUrl: AUTH_URL,
    audience: API_AUDIENCE,
    redirectUri: `${origin}${CALLBACK_PATH}`,
    storage: window.sessionStorage,
  });
}
