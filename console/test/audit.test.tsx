import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { auditFilters, exportFileName } from '../src/api/audit';
import { isAdmin } from '../src/auth/admin';
import { type Handler, json, problem } from './fakeApi';
import { VECTORS, vectorBytes } from './fixtures/auditChain';
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

  /** The API's answer to a JSON Lines export: one of the Python-made files, byte for byte. */
  function jsonl(vector: string): Handler {
    return () =>
      new Response(vectorBytes(vector), {
        status: 200,
        headers: {
          'Content-Type': 'application/x-ndjson',
          'Content-Disposition': 'attachment; filename="audit-org_0123456789abcdefghij.jsonl"',
        },
      });
  }

  /** The chain notice once it says `text`: it reads "Checking…" until the check is done. */
  function chainNotice(role: 'status' | 'alert', text: string): Promise<HTMLElement> {
    return waitFor(() => {
      const notice = screen.getByRole(role, { name: 'Hash chain check' });
      expect(notice.textContent).toContain(text);
      return notice;
    });
  }

  async function exportJsonLines() {
    await screen.findByText('1 event');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Export JSON Lines' }));
    });
  }

  it('checks the hash chain of a JSON Lines export and saves the bytes the API sent', async () => {
    start('/audit', { 'GET /v1/audit': page([CREATED]), 'GET /v1/audit/export': jsonl('good') }, signedIn());
    await exportJsonLines();
    const check = await chainNotice('status', 'Chain intact');
    expect(check.textContent).toContain('Chain intact: 5 events of org_0123456789abcdefghij, seq 1 to 5.');
    expect(check.textContent).toContain('The file starts at seq 1, so every link from the first event was checked.');
    expect(check.textContent).toContain(`Last hash ${VECTORS['good']?.report.last_hash}`);
    expect(saved).toEqual([{ name: 'audit-org_0123456789abcdefghij.jsonl', href: 'blob:console.test/1' }]);
    expect(new Uint8Array(await blobs[0]!.arrayBuffer())).toEqual(vectorBytes('good'));
    expect(screen.getByText('Saved audit-org_0123456789abcdefghij.jsonl.')).toBeDefined();
  });

  it('says from which seq an export narrowed by time was checked', async () => {
    start(
      '/audit?since=2026-10-01T09:30:10Z',
      { 'GET /v1/audit': page([CREATED]), 'GET /v1/audit/export': jsonl('starts at seq 3') },
      signedIn(),
    );
    await exportJsonLines();
    const check = await chainNotice('status', 'Chain intact: 3 events');
    expect(check.textContent).toContain(
      'The file starts at seq 3, so the links before it were not checked. Export with no From or Until to check the whole chain.',
    );
  });

  it('names the first broken link of an export that does not check out, and still saves it', async () => {
    start(
      '/audit',
      { 'GET /v1/audit': page([CREATED]), 'GET /v1/audit/export': jsonl('tampered action') },
      signedIn(),
    );
    await exportJsonLines();
    const check = await chainNotice('alert', 'Chain broken');
    expect(check.textContent).toContain(
      'Chain broken at line 2 (seq 2): its canonical bytes do not say what the row says.',
    );
    expect(check.textContent).toContain('The 1 event before it checks out.');
    expect(saved).toHaveLength(1);
    expect(new Uint8Array(await blobs[0]!.arrayBuffer())).toEqual(vectorBytes('tampered action'));
  });

  it('does not call an export filtered by action broken for the events the filter left out', async () => {
    start(
      '/audit?action=app.created',
      { 'GET /v1/audit': page([CREATED]), 'GET /v1/audit/export': jsonl('gap') },
      signedIn(),
    );
    await exportJsonLines();
    const check = await chainNotice('status', 'filtered by action');
    expect(check.textContent).toContain(
      'This export is filtered by action, actor or target, so it leaves events out and its chain cannot be checked.',
    );
    expect(check.textContent).toContain('The check stopped at line 3 (seq 3): an event is missing before this line.');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('does not check a CSV export, which carries no canonical bytes', async () => {
    start(
      '/audit',
      {
        'GET /v1/audit': page([CREATED]),
        'GET /v1/audit/export': () => new Response(csv, { status: 200, headers: { 'Content-Type': 'text/csv' } }),
      },
      signedIn(),
    );
    await screen.findByText('1 event');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }));
    });
    expect((await screen.findByRole('status')).textContent).toBe('Saved audit.csv.');
    expect(screen.queryByRole('status', { name: 'Hash chain check' })).toBeNull();
  });

  it('checks a file saved earlier without calling the API or saving anything', async () => {
    const { api } = start('/audit', { 'GET /v1/audit': page([CREATED]) }, signedIn());
    await screen.findByText('1 event');
    const input = screen.getByLabelText('Verify a file');
    const choose = async (vector: string, name: string) => {
      await act(async () => {
        fireEvent.change(input, { target: { files: [new File([vectorBytes(vector)], name)] } });
      });
    };

    await choose('gap', 'last-month.jsonl');
    const broken = await chainNotice('alert', 'Chain broken');
    expect(broken.textContent).toContain('Chain broken at line 3 (seq 3): an event is missing before this line.');
    expect(broken.textContent).toContain('The 2 events before it check out.');
    expect(broken.textContent).toContain('last-month.jsonl');

    await choose('good', 'whole.jsonl');
    await chainNotice('status', 'Chain intact: 5 events');
    expect(screen.queryByRole('alert')).toBeNull();

    await choose('empty', 'nothing.jsonl');
    await chainNotice('status', 'nothing.jsonl has no events.');
    expect(api.of('GET', '/v1/audit/export')).toHaveLength(0);
    expect(saved).toEqual([]);
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
        'People',
        'Audit log',
        'Help',
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
