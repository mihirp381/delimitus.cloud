import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { auditFilters, exportFileName } from '../src/api/audit';
import { isAdmin } from '../src/auth/admin';
import { type Handler, json, problem } from './fakeApi';
import { signedIn, start } from './harness';

const USER = 'usr_cccccccccccccccccccc';
const APP_ID = 'app_aaaaaaaaaaaaaaaaaaaa';
const ZERO = '0'.repeat(64);

function event(seq: number, overrides: Record<string, unknown> = {}) {
  return {
    seq,
    at: '2026-09-29T10:00:00Z',
    action: 'app.created',
    actor: { kind: 'user', id: USER, via_agent: false, client_id: null },
    target: { kind: 'app', id: APP_ID },
    before: null,
    after: null,
    policy_decision_id: null,
    prev_hash: ZERO,
    hash: seq.toString(16).padStart(64, '0'),
    ...overrides,
  };
}

const REMOVED_BY_AGENT = event(12, {
  action: 'grant.removed',
  actor: { kind: 'user', id: USER, via_agent: true, client_id: 'claude-code' },
  target: { kind: 'app_grant', id: 'gnt_000000000000000000o1' },
  before: { role: 'user', subject_kind: 'org', subject_id: null, environment_id: 'env_prod0000000000000000' },
  policy_decision_id: 'pdc_bbbbbbbbbbbbbbbbbbbb',
});
const CREATED = event(11, { after: { slug: 'expenses', owner_user_id: USER, status: 'active' } });

function page(events: unknown[], next: number | null = null): Handler {
  return () => json(200, { events, next_before_seq: next });
}

function entries(query: URLSearchParams): [string, string][] {
  return [...query.entries()].sort(([a], [b]) => a.localeCompare(b));
}

describe('audit log', () => {
  it('lists events newest first, marks agent actions and links app targets', async () => {
    const { api } = start('/audit', { 'GET /v1/audit': page([REMOVED_BY_AGENT, CREATED]) }, signedIn());
    await screen.findByText('2 events');
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows.map((r) => within(r).getAllByRole('cell').at(-1)?.textContent)).toEqual(['12', '11']);
    expect(within(rows[0]!).getByText('agent')).toBeTruthy();
    expect(within(rows[0]!).getByText('claude-code')).toBeTruthy();
    expect(within(rows[0]!).getByText('pdc_bbbbbbbbbbbbbbbbbbbb')).toBeTruthy();
    expect(within(rows[0]!).getByText(/"subject_kind": "org"/)).toBeTruthy();
    expect(within(rows[1]!).queryByText('agent')).toBeNull();
    expect(within(rows[1]!).getByRole('link', { name: APP_ID }).getAttribute('href')).toBe(`/apps/${APP_ID}`);
    expect(screen.queryByRole('button', { name: 'Load older events' })).toBeNull();
    expect(entries(api.of('GET', '/v1/audit')[0]!.query)).toEqual([]);
    expect(api.of('GET', '/v1/audit')[0]?.headers.get('Authorization')).toBe('Bearer tok-admin');
  });

  it('links a kill switch step to its run when the row names the app', async () => {
    const run = 'ksr_rrrrrrrrrrrrrrrrrrrr';
    const step = (seq: number, after: Record<string, unknown> | null) =>
      event(seq, { action: 'kill_switch.step', target: { kind: 'kill_switch_run', id: run }, after });
    start(
      '/audit',
      { 'GET /v1/audit': page([step(21, { app_id: APP_ID, step: 'gateway_deny' }), step(20, { app_id: '../x' }), step(19, null)]) },
      signedIn(),
    );
    await screen.findByText('3 events');
    const rows = screen.getAllByRole('row').slice(1);
    expect(within(rows[0]!).getByRole('link', { name: run }).getAttribute('href')).toBe(
      `/apps/${APP_ID}/kill-switch/${run}`,
    );
    expect(within(rows[1]!).queryByRole('link', { name: run })).toBeNull();
    expect(within(rows[2]!).queryByRole('link', { name: run })).toBeNull();
    expect(within(rows[2]!).getByText(run)).toBeTruthy();
  });

  it('applies filters through the URL and sends only the ones that are set', async () => {
    const { api, router } = start('/audit', { 'GET /v1/audit': page([CREATED]) }, signedIn());
    const form = await screen.findByRole('form', { name: 'Filter events' });
    fireEvent.change(within(form).getByLabelText('Action'), { target: { value: 'grant.removed' } });
    fireEvent.change(within(form).getByLabelText('Actor id'), { target: { value: `  ${USER} ` } });
    fireEvent.change(within(form).getByLabelText('From (inclusive)'), { target: { value: '2026-09-01T00:00' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Search' }));
    const since = new Date('2026-09-01T00:00').toISOString();
    await waitFor(() => expect(api.of('GET', '/v1/audit')).toHaveLength(2));
    expect(entries(api.of('GET', '/v1/audit')[1]!.query)).toEqual([
      ['action', 'grant.removed'],
      ['actor_id', USER],
      ['since', since],
    ]);
    expect(router.state.location.search).toEqual({ action: 'grant.removed', actor_id: USER, since });
    // The form shows the applied filters again after the navigation.
    const again = screen.getByRole('form', { name: 'Filter events' });
    expect((within(again).getByLabelText('Action') as HTMLSelectElement).value).toBe('grant.removed');
    expect((within(again).getByLabelText('From (inclusive)') as HTMLInputElement).value).toBe('2026-09-01T00:00');

    fireEvent.click(within(again).getByRole('button', { name: 'Clear filters' }));
    await waitFor(() => expect(api.of('GET', '/v1/audit')).toHaveLength(3));
    expect(entries(api.of('GET', '/v1/audit')[2]!.query)).toEqual([]);
  });

  it('drops filters from the URL that the API would refuse', async () => {
    const { api } = start(
      '/audit?action=bogus&actor_kind=operator&target_id=%20&since=yesterday&limit=9&constructor=1',
      { 'GET /v1/audit': page([]) },
      signedIn(),
    );
    expect(await screen.findByText('No event matches the filters.')).toBeTruthy();
    expect(entries(api.of('GET', '/v1/audit')[0]!.query)).toEqual([['actor_kind', 'operator']]);
  });

  it('loads older events with the cursor the API returned', async () => {
    const { api } = start(
      '/audit?target_kind=app',
      { 'GET /v1/audit': [page([REMOVED_BY_AGENT, CREATED], 11), page([event(10), event(9)])] },
      signedIn(),
    );
    expect(await screen.findByText('2 events, more are older')).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Load older events' }));
    });
    expect(await screen.findByText('4 events')).toBeTruthy();
    expect(entries(api.of('GET', '/v1/audit')[1]!.query)).toEqual([
      ['before_seq', '11'],
      ['target_kind', 'app'],
    ]);
    expect(screen.queryByRole('button', { name: 'Load older events' })).toBeNull();
  });

  it('shows the refusal a non-admin gets from the API', async () => {
    start('/audit', { 'GET /v1/audit': () => problem(403, 'FORBIDDEN', 'Org admins only') }, signedIn());
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Org admins only');
    expect(alert.textContent).toContain('req_test_0001');
  });
});

describe('audit export', () => {
  const saved: { name: string; href: string }[] = [];
  const blobs: Blob[] = [];

  beforeEach(() => {
    saved.length = 0;
    blobs.length = 0;
    // jsdom has no object URLs and does not download.
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: (blob: Blob) => {
        blobs.push(blob);
        return `blob:console.test/${blobs.length}`;
      },
    });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: () => undefined });
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click(this: HTMLAnchorElement) {
      saved.push({ name: this.download, href: this.href });
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  const csv = 'seq,at,action\n11,2026-09-29T10:00:00+00:00,app.created\n';

  it('saves the events matching the applied filters and refreshes the log', async () => {
    const { api } = start(
      '/audit?action=app.created',
      {
        'GET /v1/audit': page([CREATED]),
        'GET /v1/audit/export': () =>
          new Response(csv, {
            status: 200,
            headers: {
              'Content-Type': 'text/csv; charset=utf-8',
              'Content-Disposition': 'attachment; filename="audit-org_ffffffffffffffffffff.csv"',
            },
          }),
      },
      signedIn(),
    );
    await screen.findByText('1 event');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }));
    });
    expect((await screen.findByRole('status')).textContent).toBe('Saved audit-org_ffffffffffffffffffff.csv.');
    const [call] = api.of('GET', '/v1/audit/export');
    expect(entries(call!.query)).toEqual([
      ['action', 'app.created'],
      ['format', 'csv'],
    ]);
    expect(call?.headers.get('Authorization')).toBe('Bearer tok-admin');
    expect(saved).toEqual([{ name: 'audit-org_ffffffffffffffffffff.csv', href: 'blob:console.test/1' }]);
    expect(await blobs[0]?.text()).toBe(csv);
    // The export is itself an audit event, so the log is read again.
    await waitFor(() => expect(api.of('GET', '/v1/audit')).toHaveLength(2));
  });

  it('shows a refused export and saves nothing', async () => {
    start(
      '/audit',
      {
        'GET /v1/audit': page([CREATED]),
        'GET /v1/audit/export': () => problem(403, 'FORBIDDEN', 'Agent sessions cannot export'),
      },
      signedIn(),
    );
    await screen.findByText('1 event');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Export JSON Lines' }));
    });
    expect((await screen.findByRole('alert')).textContent).toContain('Agent sessions cannot export');
    expect(saved).toEqual([]);
    expect(screen.queryByRole('status')).toBeNull();
  });
});

describe('admin gating', () => {
  const apps: Record<string, Handler> = { 'GET /v1/apps': () => json(200, { apps: [] }) };
  const whoami = (role: string | null): Handler => () =>
    json(200, {
      org_id: 'org_ffffffffffffffffffff',
      subject: USER,
      kind: 'user',
      credential_id: 'c',
      is_agent: false,
      client_id: null,
      role,
    });

  it('answers from the role whoami gives', () => {
    const me = { org_id: 'o', subject: USER, kind: 'user', credential_id: 'c', is_agent: false, client_id: null } as const;
    expect(isAdmin({ ...me, role: 'admin' })).toBe(true);
    expect(isAdmin({ ...me, role: 'member' })).toBe(false);
    expect(isAdmin({ ...me, role: null })).toBe(false);
    expect(isAdmin(undefined)).toBe(false);
  });

  it('shows the audit log to an org admin', async () => {
    start('/', { ...apps, 'GET /v1/whoami': whoami('admin') }, signedIn());
    const nav = await screen.findByRole('navigation', { name: 'Main' });
    await waitFor(() =>
      expect(within(nav).getAllByRole('link').map((a) => a.textContent)).toEqual([
        'Apps',
        'Approvals',
        'Connections',
        'Internet access',
        'Your environment',
        'Audit log',
      ]),
    );
  });

  it.each([['member'], [null]])('hides the audit log from a caller whose role is %s', async (role) => {
    const { api } = start('/audit', { ...apps, 'GET /v1/whoami': whoami(role) }, signedIn());
    expect(await screen.findByText('Only org admins can see the audit log.')).toBeTruthy();
    const nav = screen.getByRole('navigation', { name: 'Main' });
    expect(within(nav).queryByRole('link', { name: 'Audit log' })).toBeNull();
    expect(within(nav).getByRole('link', { name: 'Approvals' })).toBeTruthy();
    expect(within(nav).getByRole('link', { name: 'Connections' })).toBeTruthy();
    expect(within(nav).getByRole('link', { name: 'Internet access' })).toBeTruthy();
    expect(api.of('GET', '/v1/audit')).toHaveLength(0);
  });

  it('waits for whoami before saying who may see the audit log', async () => {
    const { api } = start('/audit', { ...apps, 'GET /v1/whoami': () => new Promise<Response>(() => {}) }, signedIn());
    expect(await screen.findByText('Loading…')).toBeTruthy();
    expect(screen.queryByText('Only org admins can see the audit log.')).toBeNull();
    expect(api.of('GET', '/v1/audit')).toHaveLength(0);
  });

  it('shows why whoami failed instead of guessing', async () => {
    const { api } = start(
      '/audit',
      { ...apps, 'GET /v1/whoami': () => problem(429, 'RATE_LIMITED', 'Too many requests') },
      signedIn(),
    );
    expect((await screen.findByRole('alert')).textContent).toContain('Too many requests');
    expect(screen.queryByText('Only org admins can see the audit log.')).toBeNull();
    expect(api.of('GET', '/v1/audit')).toHaveLength(0);
  });

  it('hides the audit log when the seam says the caller is not an admin', async () => {
    const { api } = start('/audit', apps, signedIn(), () => false);
    expect(await screen.findByText('Only org admins can see the audit log.')).toBeTruthy();
    const nav = screen.getByRole('navigation', { name: 'Main' });
    expect(within(nav).queryByRole('link', { name: 'Audit log' })).toBeNull();
    expect(api.of('GET', '/v1/audit')).toHaveLength(0);
  });
});

describe('audit helpers', () => {
  it('keeps only filter values the API accepts', () => {
    expect(
      auditFilters({
        action: 'toString',
        actor_kind: 'hasOwnProperty',
        actor_id: 42,
        target_kind: ' app ',
        target_id: 'x'.repeat(201),
        since: '2026-09-01T12:00:00+02:00',
        until: 'not a time',
      }),
    ).toEqual({ actor_id: '42', target_kind: 'app', since: '2026-09-01T10:00:00.000Z' });
  });

  it('names the download from the API, or falls back', () => {
    expect(exportFileName('attachment; filename="audit-org_x.jsonl"', 'jsonl')).toBe('audit-org_x.jsonl');
    expect(exportFileName('attachment; filename="../../etc/passwd"', 'csv')).toBe('audit.csv');
    expect(exportFileName(null, 'jsonl')).toBe('audit.jsonl');
  });
});
