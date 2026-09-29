import { createMemoryHistory } from '@tanstack/react-router';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Grants } from '../src/api/grants';
import { createSession, type Session, STORAGE_KEY } from '../src/auth/session';
import { App, createConsole } from '../src/router';
import { fakeApi, type Handler, json, ORIGIN, problem } from './fakeApi';

const OWNER = 'usr_cccccccccccccccccccc';
const OTHER = 'usr_dddddddddddddddddddd';
const APP = {
  id: 'app_aaaaaaaaaaaaaaaaaaaa',
  slug: 'expenses',
  owner_user_id: OWNER,
  status: 'active',
  created_at: '2026-09-28T10:00:00Z',
  environments: [
    { id: 'env_preview0000000000000', name: 'preview', config_version: 1, grants_version: 0, current_deployment_id: null },
    { id: 'env_prod0000000000000000', name: 'prod', config_version: 2, grants_version: 3, current_deployment_id: 'dep_eeeeeeeeeeeeeeeeeeee' },
  ],
} as const;
const PROD_GRANTS = `/v1/apps/${APP.id}/environments/env_prod0000000000000000/grants`;
const PREVIEW_GRANTS = `/v1/apps/${APP.id}/environments/env_preview0000000000000/grants`;
const ORG_USER = { id: 'gnt_000000000000000000o1', role: 'user', subject_kind: 'org', subject_id: null } as const;
const OWNER_BUILDER = { id: 'gnt_000000000000000000b1', role: 'builder', subject_kind: 'user', subject_id: OWNER } as const;
const WHOAMI = () =>
  json(200, { org_id: 'org_ffffffffffffffffffff', subject: 'dev-admin', kind: 'user', credential_id: 'c', is_agent: false, client_id: null });

function prodGrants(version: number, list: Grants['grants']): Grants {
  return { environment_id: 'env_prod0000000000000000', grants_version: version, grants: list };
}

function start(path: string, routes: Record<string, Handler | Handler[]>, session?: Session) {
  const api = fakeApi({ 'GET /v1/whoami': WHOAMI, ...routes });
  const s = session ?? createSession(null);
  const app = createConsole({
    baseUrl: ORIGIN,
    session: s,
    fetch: api.fetch,
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(<App console={app} />);
  return { api, session: s, router: app.router };
}

function signedIn(): Session {
  const s = createSession(null);
  s.set('tok-admin', false);
  return s;
}

describe('login', () => {
  it('sends a visitor without a token to the login page and back after dev sign-in', async () => {
    const { api, router, session } = start('/apps/app_aaaaaaaaaaaaaaaaaaaa', {
      [`GET /v1/apps/${APP.id}`]: () => json(200, APP),
      [`GET ${PROD_GRANTS}`]: () => json(200, prodGrants(3, [])),
      [`GET ${PREVIEW_GRANTS}`]: () => json(200, { ...prodGrants(0, []), environment_id: 'env_preview0000000000000' }),
    });
    const box = await screen.findByLabelText('API token');
    expect(router.state.location.pathname).toBe('/login');
    fireEvent.change(box, { target: { value: '  tok-pasted \n' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('heading', { name: 'expenses' })).toBeTruthy();
    expect(router.state.location.pathname).toBe(`/apps/${APP.id}`);
    expect(session.token()).toBe('tok-pasted');
    expect(api.of('GET', '/v1/whoami')[0]?.headers.get('Authorization')).toBe('Bearer tok-pasted');
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it('keeps the token for the tab only when asked', async () => {
    const session = createSession(window.sessionStorage);
    start('/login', { 'GET /v1/apps': () => json(200, { apps: [] }) }, session);
    fireEvent.change(await screen.findByLabelText('API token'), { target: { value: 'tok-kept' } });
    fireEvent.click(screen.getByLabelText('Keep it for this tab'));
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await screen.findByRole('heading', { name: 'Apps' });
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe('tok-kept');
    expect(createSession(window.sessionStorage).token()).toBe('tok-kept');
  });

  it('refuses a token the API refuses and keeps nothing', async () => {
    const { session, router } = start('/login', {
      'GET /v1/whoami': () => problem(401, 'UNAUTHENTICATED', 'Authentication required'),
    });
    fireEvent.change(await screen.findByLabelText('API token'), { target: { value: 'bad' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Authentication required');
    expect(alert.textContent).toContain('UNAUTHENTICATED');
    expect(session.token()).toBeNull();
    expect(router.state.location.pathname).toBe('/login');
  });

  it('ignores a next path that leaves the site', async () => {
    const { router } = start('/login?next=%2F%2Fevil.example%2F', { 'GET /v1/apps': () => json(200, { apps: [] }) });
    fireEvent.change(await screen.findByLabelText('API token'), { target: { value: 'tok' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await screen.findByRole('heading', { name: 'Apps' });
    expect(router.state.location.href).toBe('/');
  });
});

describe('inventory', () => {
  const apps = [
    { id: 'app_zzzzzzzzzzzzzzzzzzzz', slug: 'timesheets', owner_user_id: OTHER, status: 'quarantined' },
    { id: APP.id, slug: 'expenses', owner_user_id: OWNER, status: 'active' },
  ];

  it('lists every app by slug with status and owner, and filters by slug or owner', async () => {
    start('/', { 'GET /v1/apps': () => json(200, { apps }) }, signedIn());
    await screen.findByRole('link', { name: 'expenses' });
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows.map((r) => within(r).getAllByRole('cell')[0]?.textContent)).toEqual(['expenses', 'timesheets']);
    expect(within(rows[1]!).getByText('quarantined')).toBeTruthy();
    expect(screen.getByText('2 apps')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Filter apps'), { target: { value: OTHER.slice(0, 10) + 'ddd' } });
    expect(screen.queryByRole('link', { name: 'expenses' })).toBeNull();
    expect(screen.getByText('1 of 2 apps')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Filter apps'), { target: { value: 'nothing' } });
    expect(screen.getByText('No app matches the filter.')).toBeTruthy();
  });

  it('says so when there are no apps', async () => {
    start('/', { 'GET /v1/apps': () => json(200, { apps: [] }) }, signedIn());
    expect(await screen.findByText(/No apps yet/)).toBeTruthy();
  });

  it('shows the refusal and its request id', async () => {
    start('/', { 'GET /v1/apps': () => problem(403, 'FORBIDDEN', 'Not allowed') }, signedIn());
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Not allowed');
    expect(alert.textContent).toContain('req_test_0001');
  });

  it('returns to the login page when the token stops working', async () => {
    const session = signedIn();
    const { router } = start('/', { 'GET /v1/apps': () => problem(401, 'UNAUTHENTICATED', 'Expired') }, session);
    await screen.findByLabelText('API token');
    expect(session.token()).toBeNull();
    expect(router.state.location.pathname).toBe('/login');
  });
});

describe('app detail', () => {
  function routes(put: Handler | Handler[], prod: Handler | Handler[]) {
    return {
      [`GET /v1/apps/${APP.id}`]: () => json(200, APP),
      [`GET ${PREVIEW_GRANTS}`]: () =>
        json(200, { environment_id: 'env_preview0000000000000', grants_version: 0, grants: [] }),
      [`GET ${PROD_GRANTS}`]: prod,
      [`PUT ${PROD_GRANTS}`]: put,
    };
  }

  it('lists production before preview, with who has access to each', async () => {
    start(`/apps/${APP.id}`, routes([], () => json(200, prodGrants(3, [OWNER_BUILDER, ORG_USER]))), signedIn());
    const headings = await screen.findAllByRole('heading', { level: 2 });
    expect(headings.map((h) => h.textContent)).toEqual(['Production prod', 'Preview preview']);
    const prod = screen.getByRole('region', { name: 'Production prod' });
    await within(prod).findByText('Everyone in the organisation');
    expect(within(prod).getByText(OWNER)).toBeTruthy();
    expect(within(prod).getByText('dep_eeeeeeeeeeeeeeeeeeee')).toBeTruthy();
    const preview = screen.getByRole('region', { name: 'Preview preview' });
    expect(await within(preview).findByText('Nobody has access yet.')).toBeTruthy();
    expect(within(preview).getByText('None yet')).toBeTruthy();
  });

  it('removes access only after the slug is typed, and survives a 412 by re-reading', async () => {
    const { api } = start(
      `/apps/${APP.id}`,
      routes(
        [
          () => problem(412, 'PRECONDITION_STALE', 'Changed since you read it'),
          ({ body }) => json(200, prodGrants(5, (body as Grants).grants as Grants['grants'])),
        ],
        [
          () => json(200, prodGrants(3, [OWNER_BUILDER, ORG_USER])),
          () => json(200, prodGrants(4, [OWNER_BUILDER, ORG_USER]), { ETag: '"4"' }),
          () => json(200, prodGrants(5, [OWNER_BUILDER])),
        ],
      ),
      signedIn(),
    );
    const prod = await screen.findByRole('region', { name: 'Production prod' });
    fireEvent.click(
      await within(prod).findByRole('button', {
        name: 'Remove access for Everyone in the organisation (user) in Production',
      }),
    );
    const dialog = await screen.findByRole('dialog', { name: 'Remove access' });
    const confirm = within(dialog).getByRole('button', { name: 'Remove access' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText(/Type/), { target: { value: 'expense' } });
    expect(confirm.disabled).toBe(true);
    expect(api.of('PUT', PROD_GRANTS)).toHaveLength(0);
    fireEvent.change(within(dialog).getByLabelText(/Type/), { target: { value: 'expenses' } });
    expect(confirm.disabled).toBe(false);
    await act(async () => {
      fireEvent.click(confirm);
    });
    expect(await within(prod).findByRole('status')).toBeTruthy();
    const puts = api.of('PUT', PROD_GRANTS);
    expect(puts.map((p) => p.headers.get('If-Match'))).toEqual(['"3"', '"4"']);
    expect(puts[1]?.body).toEqual({ grants: [{ role: 'builder', subject_kind: 'user', subject_id: OWNER }] });
    await waitFor(() => expect(within(prod).queryByText('Everyone in the organisation')).toBeNull());
    expect(within(prod).getByText(OWNER)).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('keeps the dialog open and shows the refusal when removal is refused', async () => {
    start(
      `/apps/${APP.id}`,
      routes(() => problem(403, 'FORBIDDEN', 'Only a builder can change sharing'), () =>
        json(200, prodGrants(3, [ORG_USER])),
      ),
      signedIn(),
    );
    const prod = await screen.findByRole('region', { name: 'Production prod' });
    fireEvent.click(await within(prod).findByRole('button', { name: /Remove access for/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Remove access' });
    fireEvent.change(within(dialog).getByLabelText(/Type/), { target: { value: 'expenses' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Remove access' }));
    });
    expect((await within(dialog).findByRole('alert')).textContent).toContain('Only a builder can change sharing');
    expect(within(prod).getByText('Everyone in the organisation')).toBeTruthy();
  });

  it('says when a removal waits for approval and keeps the grant listed', async () => {
    const pending = { environment_id: 'env_prod0000000000000000', grants_version: 3, approval_ids: ['apr_aaaaaaaaaaaaaaaaaaaa'] };
    start(
      `/apps/${APP.id}`,
      routes(() => json(202, pending, { ETag: '"3"' }), () => json(200, prodGrants(3, [ORG_USER]))),
      signedIn(),
    );
    const prod = await screen.findByRole('region', { name: 'Production prod' });
    fireEvent.click(await within(prod).findByRole('button', { name: /Remove access for/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Remove access' });
    fireEvent.change(within(dialog).getByLabelText(/Type/), { target: { value: 'expenses' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Remove access' }));
    });
    expect((await within(prod).findByRole('status')).textContent).toBe(
      'Waiting for approval, nothing changed yet: apr_aaaaaaaaaaaaaaaaaaaa.',
    );
    expect(within(prod).getByText('Everyone in the organisation')).toBeTruthy();
  });

  it('shows not found for an unknown app', async () => {
    start('/apps/app_unknown0000000000000', {}, signedIn());
    expect((await screen.findByRole('alert')).textContent).toContain('Not found');
  });
});
