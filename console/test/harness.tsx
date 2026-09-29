import { createMemoryHistory } from '@tanstack/react-router';
import { render } from '@testing-library/react';
import type { RouterContext } from '../src/context';
import { createSession, type Session } from '../src/auth/session';
import { App, createConsole } from '../src/router';
import { fakeApi, type Handler, json, ORIGIN } from './fakeApi';

export const WHOAMI = () =>
  json(200, {
    org_id: 'org_ffffffffffffffffffff',
    subject: 'dev-admin',
    kind: 'user',
    credential_id: 'c',
    is_agent: false,
    client_id: null,
  });

/** Renders the console at `path` against a fake API that also answers `GET /v1/whoami`. */
export function start(
  path: string,
  routes: Record<string, Handler | Handler[]>,
  session?: Session,
  isAdmin?: RouterContext['isAdmin'],
) {
  const api = fakeApi({ 'GET /v1/whoami': WHOAMI, ...routes });
  const s = session ?? createSession(null);
  const app = createConsole({
    baseUrl: ORIGIN,
    session: s,
    fetch: api.fetch,
    history: createMemoryHistory({ initialEntries: [path] }),
    isAdmin,
  });
  render(<App console={app} />);
  return { api, session: s, router: app.router, queryClient: app.queryClient };
}

export function signedIn(): Session {
  const s = createSession(null);
  s.set('tok-admin', false);
  return s;
}
