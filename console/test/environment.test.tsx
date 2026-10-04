import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { CellOut, Usage } from '../src/api/cell';
import { json, problem } from './fakeApi';
import { signedIn, start } from './harness';

const APP_ID = 'app_aaaaaaaaaaaaaaaaaaaa';
const PROD = 'env_prod0000000000000000';
const PREVIEW = 'env_preview0000000000000';
const DEPLOY = 'dep_eeeeeeeeeeeeeeeeeeee';
const APPROVAL = 'apr_ffffffffffffffffffff';
const IP = '34.120.7.9';

const member = () =>
  json(200, {
    org_id: 'org_ffffffffffffffffffff',
    subject: 'dev-member',
    kind: 'user',
    credential_id: 'c',
    is_agent: false,
    client_id: null,
    role: 'member',
  });

function resource(name: 'database' | 'egress' | 'connections', monthly: number) {
  return {
    resource: name,
    state: 'off',
    cause: null,
    monthly_usd: monthly,
    attempts: 0,
    failure_code: null,
    requested_at: null,
    started_at: null,
    ready_at: null,
    failed_at: null,
    deployment_id: null,
    approval_id: null,
  } as const;
}

function cell(database: Partial<CellOut['database']> = {}): CellOut {
  return {
    cell_label: 'acme01',
    resources: [
      {
        ...resource('database', 13),
        state: 'ready',
        cause: 'deploy',
        attempts: 1,
        requested_at: '2026-10-01T09:00:00Z',
        started_at: '2026-10-01T09:00:05Z',
        ready_at: '2026-10-01T09:11:00Z',
        deployment_id: DEPLOY,
      },
      {
        ...resource('egress', 7),
        state: 'creating',
        cause: 'egress_approved',
        attempts: 1,
        requested_at: '2026-10-02T14:00:00Z',
        approval_id: APPROVAL,
      },
      resource('connections', 0),
    ],
    environments: [
      { environment_id: PREVIEW, app_id: APP_ID, app_slug: 'expenses', name: 'preview', has_database: true },
      { environment_id: PROD, app_id: APP_ID, app_slug: 'expenses', name: 'prod', has_database: false },
    ],
    database: {
      tier: 'db-f1-micro',
      places_used: 1,
      places_total: 10,
      connection_limit: 2,
      nearly_full: false,
      tier_full_at: null,
      bigger_tier: 'db-g1-small',
      bigger_tier_monthly_usd: 26,
      ...database,
    },
  };
}

function usage(environment: string, changes: Partial<Usage> = {}): Usage {
  return {
    environment_id: environment,
    app_id: APP_ID,
    month: '2026-10',
    usage_type: 'session',
    session_hours: 1.02,
    instance_hours: 1.26,
    cold_starts: 21,
    cold_start_p50_seconds: 11,
    cold_start_p95_seconds: 20,
    small_sample: false,
    active_days: 1,
    billing: 'instance',
    ...changes,
  };
}

function environmentRoutes(ip: string | null = IP, database: Partial<CellOut['database']> = {}) {
  return {
    'GET /v1/cell': () => json(200, cell(database)),
    'GET /v1/egress': () => json(200, { hosts: [], outbound_ip: ip, proxy_address: null }),
    'GET /v1/usage': () =>
      json(200, {
        month: '2026-10',
        environments: [
          usage(PREVIEW),
          usage(PROD, {
            usage_type: 'rare',
            session_hours: 0,
            instance_hours: 0.2,
            cold_starts: 3,
            cold_start_p50_seconds: null,
            cold_start_p95_seconds: null,
            small_sample: true,
            billing: 'request',
          }),
        ],
        fixed_resources: [],
      }),
  };
}

function section(name: string): HTMLElement {
  const heading = screen.getByRole('heading', { name });
  const found = heading.closest('section');
  if (!found) throw new Error(`no section for ${name}`);
  return found;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('your environment', () => {
  it('shows the fixed IP to copy, each part with what asked for it, the database and usage', async () => {
    const writeText = vi.fn(() => Promise.resolve());
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    start('/environment', environmentRoutes(), signedIn());

    expect(await screen.findByText(IP)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Copy IP' }));
    expect(await screen.findByText('Copied.')).toBeTruthy();
    expect(writeText).toHaveBeenCalledWith(IP);

    await screen.findByRole('heading', { name: 'Parts created when first needed' });
    const rows = within(section('Parts created when first needed')).getAllByRole('row');
    const text = rows.map((r) => r.textContent ?? '');
    expect(text[1]).toContain('Database');
    expect(text[1]).toContain('ready');
    expect(text[1]).toContain(`A deploy that needed a database (deploy ${DEPLOY})`);
    expect(text[1]).toContain('about $13 a month');
    expect(text[2]).toContain('Egress proxy');
    expect(text[2]).toContain(`An allowed internet host (approval ${APPROVAL})`);
    expect(text[2]).toContain('Asked for');
    expect(text[2]).toContain('about $7 a month');
    expect(text[3]).toContain('Data gateway');
    expect(text[3]).toContain('Not asked for yet');
    expect(text[3]).toContain('nothing more');

    const database = section('Database');
    expect(database.textContent).toContain('db-f1-micro');
    expect(database.textContent).toContain('1 of 10');
    expect(database.textContent).toContain('2 for each app database, 2 of 20 taken');
    expect(within(database).queryByText(/bigger database/)).toBeNull();
    expect(within(database).getByRole('link', { name: 'expenses' }).getAttribute('href')).toBe(`/apps/${APP_ID}`);

    await screen.findByText('Instance-billed');
    const usageRows = within(section('Usage this month')).getAllByRole('row').map((r) => r.textContent ?? '');
    expect(usageRows[1]).toContain('expenses Preview');
    expect(usageRows[1]).toContain('1.02 h');
    expect(usageRows[1]).toContain('21, median 11s, slowest 5% 20s');
    expect(usageRows[2]).toContain('expenses Production');
    expect(usageRows[2]).toContain('Request-billed');
    expect(usageRows[2]).toContain('3, too few for timings');

    expect(screen.getByText(/These figures are not a bill/)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/\$50|200 apps|gateway instance/i);
  });

  it('says when there is no fixed IP yet and offers the bigger database when nearly full', async () => {
    start('/environment', environmentRoutes(null, { places_used: 8, nearly_full: true }), signedIn());
    expect(await screen.findByText(/has not told us its fixed outbound IP yet/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Copy IP' })).toBeNull();
    await screen.findByRole('heading', { name: 'Database' });
    const notice = within(section('Database')).getByRole('status');
    expect(notice.textContent).toContain('2 of 10 places are left');
    expect(notice.textContent).toContain('db-g1-small, at about $26 a month');
  });

  it('offers the bigger database once a deploy was refused with DB_TIER_FULL', async () => {
    start('/environment', environmentRoutes(IP, { tier_full_at: '2026-10-03T08:00:00Z' }), signedIn());
    await screen.findByRole('heading', { name: 'Database' });
    const notice = within(section('Database')).getByRole('status');
    expect(notice.textContent).toContain('DB_TIER_FULL');
    expect(notice.textContent).toContain('db-g1-small');
  });

  it('shows why the cell could not be read', async () => {
    start(
      '/environment',
      { ...environmentRoutes(), 'GET /v1/cell': () => problem(403, 'FORBIDDEN', 'Not allowed') },
      signedIn(),
    );
    expect((await screen.findByRole('alert')).textContent).toContain('Not allowed');
  });

  it('is for org admins only', async () => {
    const { api } = start('/environment', { ...environmentRoutes(), 'GET /v1/whoami': member }, signedIn());
    expect(await screen.findByText('Only org admins can see the environment.')).toBeTruthy();
    const nav = screen.getByRole('navigation', { name: 'Main' });
    expect(within(nav).queryByRole('link', { name: 'Your environment' })).toBeNull();
    expect(api.of('GET', '/v1/cell')).toHaveLength(0);
    expect(api.of('GET', '/v1/usage')).toHaveLength(0);
  });
});

const APP = {
  id: APP_ID,
  slug: 'expenses',
  owner_user_id: 'usr_cccccccccccccccccccc',
  status: 'active',
  created_at: '2026-09-28T10:00:00Z',
  environments: [
    { id: PREVIEW, name: 'preview', config_version: 1, grants_version: 0, current_deployment_id: null, url: null },
    { id: PROD, name: 'prod', config_version: 2, grants_version: 3, current_deployment_id: DEPLOY, url: null },
  ],
} as const;

function envPath(env: string, rest: string): string {
  return `/v1/apps/${APP_ID}/environments/${env}/${rest}`;
}

function health(env: string, state: 'running' | 'asleep' | 'failing' | null, reason: string) {
  return () =>
    json(200, { environment_id: env, state, reason, last_request_at: null, checked_at: '2026-10-03T09:00:00Z' });
}

function database(env: string, present: boolean) {
  return () =>
    json(200, {
      environment_id: env,
      present,
      database: present ? 'ssc_preview' : null,
      connection_limit: present ? 2 : null,
      pool_size: 1,
      max_instances: 1,
      size_bytes: present ? 8_598_323 : null,
      connections: present ? 1 : null,
      places_used: present ? 1 : null,
      places_total: present ? 10 : null,
      created_at: null,
      rotated_at: null,
    });
}

function grants(env: string) {
  return () => json(200, { environment_id: env, grants_version: 0, grants: [] });
}

describe('app detail', () => {
  it('shows asleep as a normal state, the month of use and the database', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      {
        [`GET /v1/apps/${APP_ID}`]: () => json(200, APP),
        [`GET ${envPath(PREVIEW, 'grants')}`]: grants(PREVIEW),
        [`GET ${envPath(PROD, 'grants')}`]: grants(PROD),
        [`GET ${envPath(PREVIEW, 'health')}`]: health(PREVIEW, 'asleep', 'idle'),
        [`GET ${envPath(PROD, 'health')}`]: health(PROD, null, 'not_deployed'),
        [`GET ${envPath(PREVIEW, 'usage')}`]: () => json(200, usage(PREVIEW)),
        [`GET ${envPath(PROD, 'usage')}`]: () => problem(403, 'FORBIDDEN', 'Not allowed'),
        [`GET ${envPath(PREVIEW, 'database')}`]: database(PREVIEW, true),
        [`GET ${envPath(PROD, 'database')}`]: database(PROD, false),
      },
      signedIn(),
    );
    const preview = (await screen.findByRole('heading', { name: /Preview/ })).closest('section');
    const prod = screen.getByRole('heading', { name: /Production/ }).closest('section');
    if (!preview || !prod) throw new Error('no environment panels');

    await waitFor(() => expect(within(preview).getByText('asleep').className).toContain('badge-neutral'));
    expect(preview.textContent).toContain('the next visit wakes it');
    await waitFor(() => expect(preview.textContent).toContain('Instance-billed, 1.02 h of sessions, 1.26 h running'));
    expect(preview.textContent).toContain('Cold starts: 21, median 11s, slowest 5% 20s.');
    await waitFor(() => expect(preview.textContent).toContain('1 connections of 2, 8.2 MB'));

    await waitFor(() => expect(prod.textContent).toContain('Not deployed yet'));
    await waitFor(() => expect(within(prod).getAllByText('Not available')).toHaveLength(1));
    await waitFor(() => expect(prod.textContent).toContain('DatabaseNone'));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(api.of('GET', envPath(PREVIEW, 'health'))).toHaveLength(1);
  });

  it('shows a failing environment in red', async () => {
    start(
      `/apps/${APP_ID}`,
      {
        [`GET /v1/apps/${APP_ID}`]: () => json(200, { ...APP, environments: [APP.environments[1]] }),
        [`GET ${envPath(PROD, 'grants')}`]: grants(PROD),
        [`GET ${envPath(PROD, 'health')}`]: health(PROD, 'failing', 'crashed'),
      },
      signedIn(),
    );
    const badge = await screen.findByText('failing');
    expect(badge.className).toContain('badge-danger');
  });
});
