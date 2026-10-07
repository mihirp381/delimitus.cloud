import { createMemoryHistory } from '@tanstack/react-router';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  challengeFor,
  CLIENT_ID,
  createOAuth,
  newPkce,
  type OAuthClient,
  PENDING_KEY,
  type Refresher,
  safeNext,
  SignInError,
  type TokenSet,
} from '../src/auth/oauth';
import { createSession, refreshDelay } from '../src/auth/session';
import { App, createConsole } from '../src/router';
import { fakeApi, json, ORIGIN } from './fakeApi';
import { WHOAMI } from './harness';

const AUTH = 'https://auth.test';
const AUDIENCE = 'https://api.test';
const REDIRECT = `${ORIGIN}/auth/callback`;

interface Sent {
  readonly url: string;
  readonly init: RequestInit;
  readonly form: URLSearchParams;
}

function tokens(n: number, expiresIn = 300): Record<string, unknown> {
  return { access_token: `acc-${n}`, refresh_token: `ref-${n}`, expires_in: expiresIn, token_type: 'Bearer' };
}

/** An OAuth client against a fake auth host; `answer` replies to every POST. */
function client(answer: (sent: Sent) => Response = () => json(200, tokens(1))) {
  const sent: Sent[] = [];
  const went: string[] = [];
  const oauth = createOAuth({
    authUrl: AUTH,
    audience: AUDIENCE,
    redirectUri: REDIRECT,
    storage: window.sessionStorage,
    fetch: async (url, init) => {
      const one = { url, init, form: new URLSearchParams(init.body as URLSearchParams) };
      sent.push(one);
      return answer(one);
    },
    go: (url) => went.push(url),
  });
  return { oauth, sent, went };
}

/** The query the auth host sends back to the callback. */
function answer(params: Record<string, string>): string {
  return `?${new URLSearchParams(params).toString()}`;
}

afterEach(() => {
  vi.useRealTimers();
});

describe('PKCE', () => {
  it('derives the S256 challenge of RFC 7636 appendix B', async () => {
    expect(await challengeFor('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk')).toBe(
      'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM',
    );
  });

  it('makes a fresh 43-character verifier with its challenge each time', async () => {
    const a = await newPkce();
    const b = await newPkce();
    expect(a.verifier).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(a.verifier).not.toBe(b.verifier);
    expect(a.challenge).toBe(await challengeFor(a.verifier));
  });

  it('sends the browser to /authorize with S256, state and the API audience', async () => {
    const { oauth, went } = client();
    await oauth.signIn('/apps');
    expect(went).toHaveLength(1);
    const url = new URL(went[0] ?? '');
    expect(`${url.origin}${url.pathname}`).toBe(`${AUTH}/authorize`);
    const q = url.searchParams;
    expect(q.get('response_type')).toBe('code');
    expect(q.get('client_id')).toBe(CLIENT_ID);
    expect(q.get('redirect_uri')).toBe(REDIRECT);
    expect(q.get('code_challenge_method')).toBe('S256');
    expect(q.get('resource')).toBe(AUDIENCE);
    expect(q.get('state')).toMatch(/^[A-Za-z0-9_-]{43}$/);
    const pending = JSON.parse(window.sessionStorage.getItem(PENDING_KEY) ?? '{}') as Record<string, string>;
    expect(pending.state).toBe(q.get('state'));
    expect(pending.next).toBe('/apps');
    expect(await challengeFor(pending.verifier ?? '')).toBe(q.get('code_challenge'));
  });
});

describe('the callback', () => {
  async function begin() {
    const fake = client();
    await fake.oauth.signIn('/apps');
    const state = new URL(fake.went[0] ?? '').searchParams.get('state') ?? '';
    const { verifier } = JSON.parse(window.sessionStorage.getItem(PENDING_KEY) ?? '{}') as { verifier: string };
    return { ...fake, state, verifier };
  }

  it('exchanges the code with its verifier, without credentials, and clears the pending sign-in', async () => {
    const { oauth, sent, state, verifier } = await begin();
    const done = await oauth.complete(answer({ code: 'c-1', state, iss: AUTH }));
    expect(done).toEqual({ tokens: { accessToken: 'acc-1', refreshToken: 'ref-1', expiresIn: 300 }, next: '/apps' });
    expect(window.sessionStorage.getItem(PENDING_KEY)).toBeNull();
    expect(sent).toHaveLength(1);
    expect(sent[0]?.url).toBe(`${AUTH}/token`);
    expect(sent[0]?.init.credentials).toBe('omit');
    expect(Object.fromEntries(sent[0]?.form ?? [])).toEqual({
      grant_type: 'authorization_code',
      client_id: CLIENT_ID,
      code: 'c-1',
      redirect_uri: REDIRECT,
      code_verifier: verifier,
      resource: AUDIENCE,
    });
    await expect(oauth.complete(answer({ code: 'c-1', state, iss: AUTH }))).rejects.toThrow(
      'No sign-in was started in this tab.',
    );
  });

  it('refuses a state that does not match, and the pending sign-in is gone after', async () => {
    const { oauth, sent, state } = await begin();
    await expect(oauth.complete(answer({ code: 'c-1', state: `${state}x`, iss: AUTH }))).rejects.toBeInstanceOf(
      SignInError,
    );
    expect(window.sessionStorage.getItem(PENDING_KEY)).toBeNull();
    expect(sent).toEqual([]);
    await expect(oauth.complete(answer({ code: 'c-1', state, iss: AUTH }))).rejects.toThrow('No sign-in');
  });

  it('refuses an answer from another issuer or without one', async () => {
    for (const iss of ['https://evil.test', '']) {
      const { oauth, sent, state } = await begin();
      const params: Record<string, string> = iss ? { code: 'c-1', state, iss } : { code: 'c-1', state };
      await expect(oauth.complete(answer(params))).rejects.toThrow('did not come from the SSC auth host');
      expect(sent).toEqual([]);
    }
  });

  it('says why the auth host sent the person back without a code', async () => {
    const { oauth, state } = await begin();
    await expect(oauth.complete(answer({ error: 'access_denied', state, iss: AUTH }))).rejects.toThrow(
      'Sign-in was cancelled.',
    );
  });

  it('refuses a token answer it does not understand or that the auth host refused', async () => {
    const odd = client(() => json(200, { access_token: 'a' }));
    await odd.oauth.signIn();
    const state = new URL(odd.went[0] ?? '').searchParams.get('state') ?? '';
    await expect(odd.oauth.complete(answer({ code: 'c', state, iss: AUTH }))).rejects.toBeInstanceOf(SignInError);
    const refused = client(() => json(400, { error: 'invalid_grant' }));
    await expect(refused.oauth.refresh('ref-1')).rejects.toThrow('refused');
  });

  it('follows only same-site paths', () => {
    expect(safeNext('/apps?x=1')).toBe('/apps?x=1');
    for (const bad of ['//evil.test', '/\\evil.test', 'https://evil.test', 3]) {
      expect(safeNext(bad)).toBeUndefined();
    }
  });
});

describe('the session', () => {
  function refresher(results: Array<TokenSet | Error>) {
    const refreshed: string[] = [];
    const revoked: string[] = [];
    const r: Refresher = {
      refresh: (token) => {
        refreshed.push(token);
        const next = results.shift();
        return next instanceof Error || next === undefined
          ? Promise.reject(next ?? new Error('none'))
          : Promise.resolve(next);
      },
      revoke: (token) => {
        revoked.push(token);
        return Promise.resolve();
      },
    };
    return { r, refreshed, revoked };
  }

  const first: TokenSet = { accessToken: 'acc-1', refreshToken: 'ref-1', expiresIn: 300 };
  const second: TokenSet = { accessToken: 'acc-2', refreshToken: 'ref-2', expiresIn: 300 };

  it('refreshes a minute before the access token expires, then again', async () => {
    vi.useFakeTimers();
    const { r, refreshed } = refresher([second, new SignInError('refused')]);
    const session = createSession(null, r);
    session.start(first);
    expect(session.token()).toBe('acc-1');
    await vi.advanceTimersByTimeAsync(239_999);
    expect(refreshed).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    expect(refreshed).toEqual(['ref-1']);
    expect(session.token()).toBe('acc-2');
    await vi.advanceTimersByTimeAsync(240_000);
    expect(refreshed).toEqual(['ref-1', 'ref-2']);
  });

  it('never refreshes sooner than halfway through a short lifetime', () => {
    expect(refreshDelay(300)).toBe(240_000);
    expect(refreshDelay(30)).toBe(15_000);
    expect(refreshDelay(0)).toBe(0);
  });

  it('ends when a refresh fails and says so', async () => {
    vi.useFakeTimers();
    const { r } = refresher([new SignInError('refused')]);
    const session = createSession(null, r);
    const lost = vi.fn();
    session.onLost(lost);
    session.start(first);
    await vi.advanceTimersByTimeAsync(240_000);
    expect(lost).toHaveBeenCalledOnce();
    expect(session.token()).toBeNull();
  });

  it('drops a refresh that answers after sign-out, and sign-out revokes', async () => {
    vi.useFakeTimers();
    let release: (t: TokenSet) => void = () => undefined;
    const revoked: string[] = [];
    const session = createSession(null, {
      refresh: () => new Promise<TokenSet>((resolve) => (release = resolve)),
      revoke: (token) => {
        revoked.push(token);
        return Promise.resolve();
      },
    });
    session.start(first);
    await vi.advanceTimersByTimeAsync(240_000);
    await session.signOut();
    expect(revoked).toEqual(['ref-1']);
    release(second);
    await vi.advanceTimersByTimeAsync(600_000);
    expect(session.token()).toBeNull();
  });

  it('keeps production tokens out of storage', () => {
    const { r } = refresher([]);
    const session = createSession(window.sessionStorage, r);
    session.start(first);
    expect(window.sessionStorage.length).toBe(0);
    session.clear();
  });
});

describe('sign-in screens', () => {
  function mount(path: string, oauth: OAuthClient) {
    const api = fakeApi({ 'GET /v1/whoami': WHOAMI, 'GET /v1/apps': () => json(200, { apps: [] }) });
    const session = createSession(null, oauth);
    const app = createConsole({
      baseUrl: ORIGIN,
      session,
      oauth,
      fetch: api.fetch,
      history: createMemoryHistory({ initialEntries: [path] }),
    });
    render(<App console={app} />);
    return { api, session, router: app.router };
  }

  it('sends the person to the auth host from the login page with where they were going', async () => {
    const { oauth, went } = client();
    mount('/apps/app_aaaaaaaaaaaaaaaaaaaa', oauth);
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with your work account' }));
    await waitFor(() => expect(went).toHaveLength(1));
    const pending = JSON.parse(window.sessionStorage.getItem(PENDING_KEY) ?? '{}') as { next: string };
    expect(pending.next).toBe('/apps/app_aaaaaaaaaaaaaaaaaaaa');
  });

  it('finishes sign-in on /auth/callback and goes where the person was going', async () => {
    const { oauth, went } = client();
    await oauth.signIn('/approvals');
    const state = new URL(went[0] ?? '').searchParams.get('state') ?? '';
    const { api, session, router } = mount(`/auth/callback${answer({ code: 'c-1', state, iss: AUTH })}`, oauth);
    await waitFor(() => expect(router.state.location.pathname).toBe('/approvals'));
    expect(session.token()).toBe('acc-1');
    await waitFor(() => expect(api.of('GET', '/v1/whoami').length).toBeGreaterThan(0));
    expect(api.of('GET', '/v1/whoami')[0]?.headers.get('Authorization')).toBe('Bearer acc-1');
    session.clear();
  });

  it('shows why a callback was refused and signs nobody in', async () => {
    const { oauth, sent } = client();
    await oauth.signIn('/');
    const { session } = mount(`/auth/callback${answer({ code: 'c-1', state: 'forged', iss: AUTH })}`, oauth);
    expect((await screen.findByRole('alert')).textContent).toContain('not for the sign-in started in this tab');
    expect(session.token()).toBeNull();
    expect(sent).toEqual([]);
    expect(screen.getByRole('link', { name: 'Try again' })).toBeTruthy();
  });

  it('revokes at the auth host on sign-out', async () => {
    const { oauth, sent } = client((s) => (s.url.endsWith('/revoke') ? new Response(null, { status: 200 }) : json(200, tokens(1))));
    const { session, router } = mount('/', oauth);
    act(() => session.start({ accessToken: 'acc-1', refreshToken: 'ref-1', expiresIn: 300 }));
    await act(() => router.navigate({ to: '/' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }));
    await waitFor(() => expect(sent.map((s) => s.url)).toEqual([`${AUTH}/revoke`]));
    expect(Object.fromEntries(sent[0]?.form ?? [])).toEqual({ token: 'ref-1', token_type_hint: 'refresh_token' });
    expect(session.token()).toBeNull();
    await waitFor(() => expect(router.state.location.pathname).toBe('/login'));
  });
});
