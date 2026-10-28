import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { CellOut, Usage } from '../src/api/cell';
import type { WarmOut } from '../src/api/warm';
import { openSection } from './appPage';
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

function warm(changes: Partial<WarmOut> = {}): WarmOut {
  return {
    environments: [
      {
        environment_id: PROD,
        app_id: APP_ID,
        app_slug: 'expenses',
        warm: false,
        suggested: true,
        opened_days: 17,
        cold_start_days: 12,
      },
    ],
    gateway: { warm: false, state: 'off', failure_code: null },
    working_days: 20,
    environment_monthly_usd: 10,
    gateway_monthly_usd: 10,
    monthly_usd: 0,
    ...changes,
  };
}

function environmentRoutes(ip: string | null = IP, database: Partial<CellOut['database']> = {}) {
  return {
    'GET /v1/cell': () => json(200, cell(database)),
    'GET /v1/egress': () => json(200, { hosts: [], outbound_ip: ip, proxy_address: null }),
    'GET /v1/warm': () => json(200, warm()),
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
  it('shows plan and usage, each part with what asked for it behind Details, the database, and never the IP', async () => {
    start('/environment', environmentRoutes(), signedIn());

    await screen.findByText('Instance-billed');
    const plan = section('Plan and usage');
    await waitFor(() => expect(plan.textContent).toContain('Added a month: $20, by the parts asked for and the warm option.'));
    const usageTable = within(plan).getByRole('table', { name: 'Usage of each app this month' });
    expect(within(usageTable).getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'App',
      'Billed',
      'Session hours',
      'Cold starts',
    ]);
    const usageRows = within(usageTable).getAllByRole('row').map((r) => r.textContent ?? '');
    expect(usageRows[1]).toContain('expenses Preview');
    expect(usageRows[1]).toContain('1.02 h');
    expect(usageRows[1]).toContain('21, median 11s, slowest 5% 20s');
    expect(usageRows[2]).toContain('expenses Production');
    expect(usageRows[2]).toContain('Request-billed');
    expect(usageRows[2]).toContain('3, too few for timings');

    const details = await openSection(plan, 'Details');
    const more = within(details).getByRole('table', { name: 'More usage figures this month' });
    expect(within(more).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['App', 'Instance hours', 'Kind of use']);
    expect(within(more).getAllByRole('row')[2]?.textContent).toContain('rare');
    const rows = within(details).getByRole('table', { name: 'Parts of your cell created when first needed' }).querySelectorAll('tr');
    const text = [...rows].map((r) => r.textContent ?? '');
    expect(text[1]).toContain('Database');
    expect(text[1]).toContain('ready');
    expect(text[1]).toContain(`A deploy that needed a database (deploy ${DEPLOY})`);
    expect(text[1]).toContain('about $13 a month');
    expect(text[2]).toContain('Egress proxy');
    expect(text[2]).toContain('An allowed internet host (approval)');
    expect(within(rows[2]!).getByRole('link', { name: 'approval' }).getAttribute('href')).toBe(`/approvals/${APPROVAL}`);
    expect(text[2]).not.toContain(APPROVAL);
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
    const meter = within(database).getByRole('img', { name: '1 of 10 database places used' });
    expect(meter.getAttribute('class')).toBe('meter');
    expect(meter.querySelector('rect')?.getAttribute('width')).toBe('10%');

    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual(['Plan and usage', 'Capacity', 'Database']);
    expect(document.body.textContent?.match(/not a bill/gi)).toHaveLength(1);
    expect(document.body.textContent).not.toContain(IP);
    expect(document.body.textContent).not.toMatch(/outbound IP/i);
    expect(document.body.textContent).not.toMatch(/\$50|200 apps|gateway instance/i);
  });

  it('shows the places meter in the warning tone when every place is taken', async () => {
    start(
      '/environment',
      {
        ...environmentRoutes(null, { places_used: 10, nearly_full: true }),
        'GET /v1/warm': () =>
          json(200, {
            ...warm({ monthly_usd: 20, gateway: { warm: true, state: 'on', failure_code: null } }),
            environments: warm().environments.map((e) => ({ ...e, warm: true })),
          }),
      },
      signedIn(),
    );
    await screen.findByRole('heading', { name: 'Database' });
    await waitFor(() => expect(section('Plan and usage').textContent).toContain('Added a month: $40'));
    const meter = within(section('Database')).getByRole('img', { name: '10 of 10 database places used' });
    expect(meter.getAttribute('class')).toBe('meter meter-warning');
    expect(meter.querySelector('rect')?.getAttribute('width')).toBe('100%');
  });

  it('offers the bigger database when nearly full', async () => {
    start('/environment', environmentRoutes(null, { places_used: 8, nearly_full: true }), signedIn());
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
    expect(notice.textContent).toContain('deleting an app');
  });

  it('shows why the cell could not be read', async () => {
    start(
      '/environment',
      { ...environmentRoutes(), 'GET /v1/cell': () => problem(403, 'FORBIDDEN', 'Not allowed') },
      signedIn(),
    );
    expect((await screen.findByRole('alert')).textContent).toContain('Not allowed');
  });

  it('shows the warm option, its cost before saving, and offers the gateway in the same step', async () => {
    const saved = warm({
      environments: [{ ...warm().environments[0]!, warm: true, suggested: false }],
      gateway: { warm: true, state: 'turning_on', failure_code: null },
      monthly_usd: 20,
    });
    const { api } = start(
      '/environment',
      { ...environmentRoutes(), 'PUT /v1/warm': () => json(200, saved) },
      signedIn(),
    );
    await screen.findByRole('heading', { name: 'Capacity' });
    const panel = section('Capacity');
    const app = await within(panel).findByRole('checkbox', { name: /expenses/ });
    expect(app.closest('label')?.textContent).toContain('suggested');
    expect(app.closest('label')?.textContent).toContain('used on 17 of 20 working days lately, with cold starts on 12');
    expect(panel.textContent).toContain('Preview apps are never warm');
    expect(panel.textContent).toContain('the warm option adds nothing more');
    const save = within(panel).getByRole('button', { name: 'Save warm option' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);

    fireEvent.click(app);
    expect(panel.textContent).toContain('the warm option adds about $10 a month');
    const note = within(panel).getByRole('status');
    expect(note.textContent).toContain('A warm app behind a sleeping gateway still waits');
    fireEvent.click(within(note).getByRole('button', { name: 'Keep the gateway warm too' }));
    const gateway = within(panel).getByRole('checkbox', { name: /Keep the gateway warm too/ }) as HTMLInputElement;
    expect(gateway.checked).toBe(true);
    expect(within(panel).queryByText(/sleeping gateway/)).toBeNull();
    expect(panel.textContent).toContain('the warm option adds about $20 a month');
    expect(api.of('PUT', '/v1/warm')).toHaveLength(0);

    await act(async () => {
      fireEvent.click(save);
    });
    const [put] = api.of('PUT', '/v1/warm');
    expect(put?.body).toEqual({ environment_ids: [PROD], gateway: true, monthly_usd_shown: 20 });
    await waitFor(() => expect(section('Capacity').textContent).toContain('being kept warm'));
    expect((within(section('Capacity')).getByRole('checkbox', { name: /expenses/ }) as HTMLInputElement).checked).toBe(
      true,
    );
  });

  it('shows why the warm option was refused and a gateway that could not be changed', async () => {
    start(
      '/environment',
      {
        ...environmentRoutes(),
        'GET /v1/warm': () =>
          json(200, warm({ gateway: { warm: true, state: 'failed', failure_code: 'CELL_DEPLOYER_FAILED' } })),
        'PUT /v1/warm': () => problem(403, 'AGENT_SESSION_REFUSED', 'Not from an agent session'),
      },
      signedIn(),
    );
    await screen.findByRole('heading', { name: 'Capacity' });
    const panel = section('Capacity');
    await waitFor(() => expect(panel.textContent).toContain('could not be changed'));
    expect(panel.textContent).toContain('CELL_DEPLOYER_FAILED');
    fireEvent.click(within(panel).getByRole('checkbox', { name: /expenses/ }));
    await act(async () => {
      fireEvent.click(within(panel).getByRole('button', { name: 'Save warm option' }));
    });
    expect((await within(panel).findByRole('alert')).textContent).toContain('Not from an agent session');
  });

  it('is for org admins only', async () => {
    const { api } = start('/environment', { ...environmentRoutes(), 'GET /v1/whoami': member }, signedIn());
    expect(await screen.findByText('Only org admins can see usage.')).toBeTruthy();
    const nav = screen.getByRole('navigation', { name: 'Main' });
    expect(within(nav).queryByRole('link', { name: 'Usage' })).toBeNull();
    expect(api.of('GET', '/v1/cell')).toHaveLength(0);
    expect(api.of('GET', '/v1/usage')).toHaveLength(0);
    expect(api.of('GET', '/v1/warm')).toHaveLength(0);
  });
});

const APP = {
  id: APP_ID,
  slug: 'expenses',
  owner_user_id: 'usr_cccccccccccccccccccc',
  status: 'active',
  created_at: '2026-09-28T10:00:00Z',
  environments: [
    { id: PREVIEW, name: 'preview', config_version: 1, form_targets: [], grants_version: 0, current_deployment_id: null, url: null },
    { id: PROD, name: 'prod', config_version: 2, form_targets: [], grants_version: 3, current_deployment_id: DEPLOY, url: null },
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
        [`GET ${envPath(PROD, 'deployments')}`]: () =>
          json(200, {
            environment_id: PROD,
            items: [
              {
                release_number: 7,
                current: true,
                started_at: '2026-10-07T10:00:00Z',
                finished_at: null,
                actor: { kind: 'user', id: 'usr_cccccccccccccccccccc', via_agent: false, name: 'Olive Owner' },
              },
            ],
          }),
      },
      signedIn(),
    );
    const preview = (await screen.findByRole('heading', { name: /Preview/ })).closest('section');
    const prod = screen.getByRole('heading', { name: /Production/ }).closest('section');
    if (!preview || !prod) throw new Error('no environment panels');

    await waitFor(() => expect(within(preview).getByText('asleep').className).toContain('badge-neutral'));
    expect(api.of('GET', envPath(PREVIEW, 'usage'))).toHaveLength(0);
    const previewDetails = await openSection(preview, 'Details');
    expect(previewDetails.textContent).toContain('the next visit wakes it');
    await waitFor(() => expect(previewDetails.textContent).toContain('Instance-billed, 1.02 h of sessions, 1.26 h running'));
    expect(previewDetails.textContent).toContain('Cold starts: 21, median 11s, slowest 5% 20s.');
    await waitFor(() => expect(previewDetails.textContent).toContain('1 connections of 2, 8.2 MB'));

    await waitFor(() => expect(within(prod).getByText('Not deployed yet').className).toContain('badge-neutral'));
    await waitFor(() => expect(prod.textContent).toContain('Release 7 by Olive Owner'));
    expect(api.of('GET', envPath(PROD, 'deployments'))[0]?.query.get('limit')).toBe('50');
    expect(api.of('GET', envPath(PREVIEW, 'deployments'))).toHaveLength(0);
    const prodDetails = await openSection(prod, 'Details');
    await waitFor(() => expect(prodDetails.textContent).toContain('ReleaseRelease 7'));
    await waitFor(() => expect(within(prodDetails).getAllByText('Not available')).toHaveLength(1));
    await waitFor(() => expect(prodDetails.textContent).toContain('DatabaseNone'));
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
