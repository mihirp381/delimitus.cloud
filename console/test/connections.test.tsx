import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { APP_PATH, envPanel, FIRST_RENDER_MS, OWNER, openSection, ORG_USER, page, PROD, whoami } from './appPage';
import { type Call, json, problem } from './fakeApi';
import { signedIn, start } from './harness';

const GROUP = 'grp_ffffffffffffffffffff';
const HOST = 'db.internal.example.com';
const DATABASE = 'ledger_main';

function connection(name: string, extra: Record<string, unknown> = {}) {
  return {
    id: `con_${name.padEnd(20, '0').slice(0, 20)}`,
    name,
    kind: 'postgres',
    owner_user_id: OWNER,
    classification: 'internal',
    ceiling: { audience: 'org', subjects: [] },
    allowed_schemas: ['public'],
    limits: { max_rows: 1000 },
    setup_status: 'ready',
    status: 'active',
    created_at: '2026-10-01T10:00:00Z',
    updated_at: '2026-10-01T10:00:00Z',
    ...extra,
  };
}

const LEDGER = connection('ledger');
const CRM = connection('crm', { classification: 'confidential', ceiling: { audience: 'subjects', subjects: [{ kind: 'group', id: GROUP }] }, setup_status: 'pending' });
const ENV_CONNECTIONS = `${APP_PATH}/environments/${PROD}/connections`;

function linked(...list: ReturnType<typeof connection>[]) {
  return {
    connections: list.map((c) => ({
      environment_id: PROD,
      connection: c,
      limits: {},
      over_ceiling_since: null,
      granted_at: '2026-10-02T10:00:00Z',
    })),
  };
}

function created({ body }: Call) {
  const b = body as Record<string, unknown>;
  return json(201, connection(b.name as string, { classification: b.classification, ceiling: b.ceiling, setup_status: 'pending' }));
}

async function connectionsPage(routes: Record<string, (c: Call) => Response | Promise<Response>>, role: 'admin' | 'member' = 'admin') {
  const result = start('/connections', { 'GET /v1/whoami': whoami(role), 'GET /v1/connections': () => json(200, { connections: [LEDGER, CRM] }), ...routes }, signedIn());
  await screen.findByRole('heading', { name: 'Connections', level: 1 }, { timeout: FIRST_RENDER_MS });
  await screen.findByText('ledger');
  return result;
}

describe('connections page', () => {
  it('lists every connection the caller may see, never an address', async () => {
    await connectionsPage({});
    const table = screen.getByRole('table', { name: 'Connections' });
    expect(within(table).getByText('crm')).toBeTruthy();
    expect(within(table).getByText('1 listed group or person')).toBeTruthy();
    expect(within(table).getAllByText('Rows per query 1000')).toHaveLength(2);
    expect(within(table).getByText('pending')).toBeTruthy();
    expect(document.body.textContent).not.toContain('host');
  });

  it('adds a connection with its address in the body only, then forgets the address', async () => {
    const { api, queryClient } = await connectionsPage({
      'GET /v1/users': () =>
        json(200, { users: [{ id: OWNER, display_name: 'Olu', email: 'olu@example.com', role: 'member', status: 'active' }] }),
      'POST /v1/connections': created,
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add a connection' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add a connection' });
    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: 'warehouse' } });
    fireEvent.change(within(dialog).getByLabelText('Classification'), { target: { value: 'restricted' } });
    fireEvent.change(within(dialog).getByLabelText('Owner: email or usr_ id'), { target: { value: 'olu@example.com' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Find' }));
    });
    await within(dialog).findByRole('radio', { name: /Olu/ });
    fireEvent.change(within(dialog).getByLabelText('Host'), { target: { value: HOST } });
    fireEvent.change(within(dialog).getByLabelText('Port'), { target: { value: '6432' } });
    fireEvent.change(within(dialog).getByLabelText('Database'), { target: { value: DATABASE } });
    fireEvent.change(within(dialog).getByLabelText(/^Schemas/), { target: { value: 'public, finance' } });
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Only the groups and people listed' }));
    fireEvent.change(within(dialog).getByLabelText(/one grp_ or usr_ id per line/), { target: { value: GROUP } });
    fireEvent.change(within(dialog).getByLabelText('Rows per query'), { target: { value: '500' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Add connection' }));
    });
    const post = api.of('POST', '/v1/connections')[0];
    expect(post?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(post?.body).toEqual({
      name: 'warehouse',
      kind: 'postgres',
      owner_user_id: OWNER,
      classification: 'restricted',
      ceiling: { audience: 'subjects', subjects: [{ kind: 'group', id: GROUP }] },
      allowed_schemas: ['public', 'finance'],
      limits: { max_rows: 500 },
      host: HOST,
      port: 6432,
      database: DATABASE,
    });
    expect((await screen.findByRole('status')).textContent).toContain('Added warehouse.');
    expect(screen.queryByRole('dialog')).toBeNull();
    // The address reached nothing but that one request body.
    for (const call of api.calls) expect(`${call.path}?${call.query.toString()}`).not.toContain(HOST);
    const cached = JSON.stringify(queryClient.getQueryCache().getAll().map((q) => [q.queryKey, q.state.data]));
    expect(cached).not.toContain(HOST);
    expect(cached).not.toContain(DATABASE);
    fireEvent.click(screen.getByRole('button', { name: 'Add a connection' }));
    const again = await screen.findByRole('dialog', { name: 'Add a connection' });
    expect((within(again).getByLabelText('Host') as HTMLInputElement).value).toBe('');
    expect((within(again).getByLabelText('Database') as HTMLInputElement).value).toBe('');
  });

  it('clears the address once sent even when the API refuses, and shows why', async () => {
    const { api } = await connectionsPage({
      'POST /v1/connections': () => problem(422, 'CEILING_REQUIRED', 'This connection needs an audience ceiling.'),
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add a connection' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add a connection' });
    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: 'warehouse' } });
    fireEvent.change(within(dialog).getByLabelText('Owner: email or usr_ id'), { target: { value: OWNER } });
    fireEvent.change(within(dialog).getByLabelText('Host'), { target: { value: HOST } });
    fireEvent.change(within(dialog).getByLabelText('Database'), { target: { value: DATABASE } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Add connection' }));
    });
    expect(api.of('POST', '/v1/connections')).toHaveLength(1);
    expect((await within(dialog).findByRole('alert')).textContent).toContain('CEILING_REQUIRED');
    expect((within(dialog).getByLabelText('Host') as HTMLInputElement).value).toBe('');
    expect((within(dialog).getByLabelText('Database') as HTMLInputElement).value).toBe('');
    expect(within(dialog).getByText(/enter it again to retry/)).toBeTruthy();
    expect((within(dialog).getByRole('button', { name: 'Add connection' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('changes only what was edited, and sends the ceiling with a move to confidential', async () => {
    const { api } = await connectionsPage({
      'PATCH /v1/connections/ledger': ({ body }) => json(200, { ...LEDGER, ...(body as object) }),
    });
    fireEvent.click(screen.getByRole('button', { name: 'Change ledger' }));
    const dialog = await screen.findByRole('dialog', { name: 'Change ledger' });
    fireEvent.change(within(dialog).getByLabelText('Classification'), { target: { value: 'confidential' } });
    fireEvent.change(within(dialog).getByLabelText('Setup'), { target: { value: 'pending' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));
    });
    expect(api.of('PATCH', '/v1/connections/ledger')[0]?.body).toEqual({
      classification: 'confidential',
      ceiling: { audience: 'org' },
      setup_status: 'pending',
    });
    expect((await screen.findByRole('status')).textContent).toBe('Changed ledger.');
  });

  it('sends nothing when nothing changed', async () => {
    const { api } = await connectionsPage({});
    fireEvent.click(screen.getByRole('button', { name: 'Change crm' }));
    const dialog = await screen.findByRole('dialog', { name: 'Change crm' });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));
    });
    expect(api.of('PATCH', '/v1/connections/crm')).toHaveLength(0);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('shows a member the list without any way to change it', async () => {
    await connectionsPage({}, 'member');
    expect(screen.getByText(/Only org admins can add or change connections/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Add a connection' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Change ledger' })).toBeNull();
  });
});

describe("an environment's data connections", () => {
  it('reads nothing until the section is opened', async () => {
    const { api } = start(`/apps/${'app_aaaaaaaaaaaaaaaaaaaa'}`, page({ [`GET ${ENV_CONNECTIONS}`]: () => json(200, linked(LEDGER)) }), signedIn());
    const panel = await envPanel('Production');
    await within(panel).findByText('Everyone in the organisation');
    expect(api.of('GET', ENV_CONNECTIONS)).toHaveLength(0);
    await openSection(panel, 'Data connections');
    expect(await within(panel).findByText('ledger')).toBeTruthy();
    expect(api.of('GET', ENV_CONNECTIONS)).toHaveLength(1);
  });

  it('links a connection with an empty body and shows the new list', async () => {
    const { api } = start(
      `/apps/app_aaaaaaaaaaaaaaaaaaaa`,
      page({
        [`GET ${ENV_CONNECTIONS}`]: () => json(200, linked(LEDGER)),
        'GET /v1/connections': () => json(200, { connections: [LEDGER, CRM] }),
        [`PUT ${ENV_CONNECTIONS}/crm`]: () => json(200, linked(LEDGER, CRM)),
      }),
      signedIn(),
    );
    const panel = await envPanel('Production');
    await openSection(panel, 'Data connections');
    const select = (await within(panel).findByLabelText('Connection to link')) as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual(['', 'crm']);
    fireEvent.change(select, { target: { value: 'crm' } });
    await act(async () => {
      fireEvent.click(within(panel).getByRole('button', { name: 'Link connection' }));
    });
    expect(api.of('PUT', `${ENV_CONNECTIONS}/crm`)[0]?.body).toEqual({});
    expect((await within(panel).findByRole('status')).textContent).toBe('Linked crm.');
    expect(within(panel).getByRole('table', { name: 'Data connections of Production' }).textContent).toContain('crm');
  });

  it('asks for an exceed_ceiling approval naming the connection and the grants as they are', async () => {
    const { api } = start(
      `/apps/app_aaaaaaaaaaaaaaaaaaaa`,
      page({
        [`GET ${ENV_CONNECTIONS}`]: () => json(200, linked()),
        'GET /v1/connections': () => json(200, { connections: [CRM] }),
        [`PUT ${ENV_CONNECTIONS}/crm`]: () => problem(409, 'APPROVAL_REQUIRED', 'This change needs an approval first.'),
        'POST /v1/approvals': ({ body }) => json(201, { id: 'apr_cccccccccccccccccccc', state: 'pending', ...(body as object) }),
      }),
      signedIn(),
    );
    const panel = await envPanel('Production');
    await openSection(panel, 'Data connections');
    fireEvent.change(await within(panel).findByLabelText('Connection to link'), { target: { value: 'crm' } });
    await act(async () => {
      fireEvent.click(within(panel).getByRole('button', { name: 'Link connection' }));
    });
    expect((await within(panel).findByRole('alert')).textContent).toContain('APPROVAL_REQUIRED');
    expect(within(panel).getByRole('note').textContent).toContain('shared wider than crm allows');
    await act(async () => {
      fireEvent.click(within(panel).getByRole('button', { name: 'Ask for approval' }));
    });
    expect(api.of('POST', '/v1/approvals')[0]?.body).toEqual({
      environment_id: PROD,
      kind: 'exceed_ceiling',
      payload: { connection: 'crm', grants: [{ role: ORG_USER.role, subject_kind: 'org' }] },
    });
    const link = await within(panel).findByRole('link', { name: 'apr_cccccccccccccccccccc' });
    expect(link.getAttribute('href')).toBe('/approvals/apr_cccccccccccccccccccc');
  });

  it('unlinks only after the slug is typed', async () => {
    const { api } = start(
      `/apps/app_aaaaaaaaaaaaaaaaaaaa`,
      page({
        [`GET ${ENV_CONNECTIONS}`]: () => json(200, linked(LEDGER)),
        'GET /v1/connections': () => json(200, { connections: [LEDGER] }),
        [`DELETE ${ENV_CONNECTIONS}/ledger`]: () => json(200, linked()),
      }),
      signedIn(),
    );
    const panel = await envPanel('Production');
    await openSection(panel, 'Data connections');
    fireEvent.click(await within(panel).findByRole('button', { name: 'Unlink ledger from Production' }));
    const dialog = await screen.findByRole('dialog', { name: 'Unlink ledger' });
    const confirm = within(dialog).getByRole('button', { name: 'Unlink' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText(/to confirm/), { target: { value: 'expenses' } });
    await act(async () => {
      fireEvent.click(confirm);
    });
    expect(api.of('DELETE', `${ENV_CONNECTIONS}/ledger`)).toHaveLength(1);
    await waitFor(() => expect(within(panel).getByText('This environment reaches no data connection.')).toBeTruthy());
  });

  it('shows a member the links without the controls, and flags one shared too wide', async () => {
    start(
      `/apps/app_aaaaaaaaaaaaaaaaaaaa`,
      page({
        'GET /v1/whoami': whoami('member'),
        [`GET ${ENV_CONNECTIONS}`]: () =>
          json(200, { connections: [{ ...linked(CRM).connections[0], over_ceiling_since: '2026-10-03T10:00:00Z' }] }),
      }),
      signedIn(),
    );
    const panel = await envPanel('Production');
    await openSection(panel, 'Data connections');
    expect(await within(panel).findByText(/shared wider than allowed since/)).toBeTruthy();
    expect(within(panel).getByText('Only org admins can link and unlink connections.')).toBeTruthy();
    expect(within(panel).queryByRole('button', { name: /Unlink/ })).toBeNull();
  });

  it('asks for the columns only once a row is opened, and lists them', async () => {
    const schema = {
      connection: 'ledger',
      kind: 'postgres',
      tables: [
        {
          name: 'reporting.orders',
          columns: [
            { name: 'id', type: 'integer', db_type: 'int8' },
            { name: 'total', type: 'decimal', db_type: null },
          ],
        },
      ],
      snapshot_version: 12,
      cached: true,
    };
    const { api } = start(
      `/apps/app_aaaaaaaaaaaaaaaaaaaa`,
      page({
        [`GET ${ENV_CONNECTIONS}`]: () => json(200, linked(LEDGER)),
        'GET /v1/connections': () => json(200, { connections: [LEDGER] }),
        [`GET ${ENV_CONNECTIONS}/ledger/schema`]: () => json(200, schema),
      }),
      signedIn(),
    );
    const panel = await envPanel('Production');
    await openSection(panel, 'Data connections');
    const row = (await within(panel).findByText('ledger')).closest('tr');
    if (!row) throw new Error('no row');
    expect(api.of('GET', `${ENV_CONNECTIONS}/ledger/schema`)).toHaveLength(0);
    await openSection(row, 'Columns');
    const table = await within(panel).findByRole('region', { name: 'reporting.orders' });
    expect(table.textContent).toContain('id integer (int8)');
    expect(table.textContent).toContain('total decimal');
    expect(within(panel).getByText(/Snapshot 12, as read in the last five minutes/)).toBeTruthy();
    expect(api.of('GET', `${ENV_CONNECTIONS}/ledger/schema`)).toHaveLength(1);
  });

  it("shows the gateway's refusal with its fix", async () => {
    start(
      `/apps/app_aaaaaaaaaaaaaaaaaaaa`,
      page({
        [`GET ${ENV_CONNECTIONS}`]: () => json(200, linked(CRM)),
        'GET /v1/connections': () => json(200, { connections: [CRM] }),
        [`GET ${ENV_CONNECTIONS}/crm/schema`]: () =>
          problem(409, 'CONNECTION_NOT_GRANTED', 'This environment cannot use that connection yet.'),
      }),
      signedIn(),
    );
    const panel = await envPanel('Production');
    await openSection(panel, 'Data connections');
    const row = (await within(panel).findByText('crm')).closest('tr');
    if (!row) throw new Error('no row');
    await openSection(row, 'Columns');
    expect((await within(panel).findByRole('alert')).textContent).toContain('CONNECTION_NOT_GRANTED');
  });
});
