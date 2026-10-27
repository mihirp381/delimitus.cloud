import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { APP_PATH, envPanel, FIRST_RENDER_MS, OWNER, openSection, ORG_USER, page, PROD, whoami } from './appPage';
import { type Call, json, problem } from './fakeApi';
import { signedIn, start, toast } from './harness';

const GROUP = 'grp_ffffffffffffffffffff';
const OWNER_TWO = 'usr_dddddddddddddddddddd';
const HOST = 'db.internal.example.com';
const DATABASE = 'ledger_main';

function connection(name: string, extra: Record<string, unknown> = {}) {
  return {
    id: `con_${name.padEnd(20, '0').slice(0, 20)}`,
    name,
    kind: 'postgres',
    owner_user_id: OWNER,
    owner_name: 'Olive Owner',
    owner_email: 'olive@example.com',
    classification: 'internal',
    ceiling: { audience: 'org', subjects: [] },
    allowed_schemas: ['public'],
    limits: { max_rows: 1000 },
    setup_status: 'ready',
    status: 'active',
    preview_access: { who: 'builders', group_id: null },
    preview_access_group_name: null,
    ceiling_subject_names: {},
    created_at: '2026-10-01T10:00:00Z',
    updated_at: '2026-10-01T10:00:00Z',
    ...extra,
  };
}

const LEDGER = connection('ledger');
const CRM = connection('crm', { classification: 'confidential', ceiling: { audience: 'subjects', subjects: [{ kind: 'group', id: GROUP }] }, setup_status: 'pending', preview_access: { who: 'group', group_id: GROUP }, preview_access_group_name: 'Finance', ceiling_subject_names: { [GROUP]: 'Finance' } });
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

async function connectionsPage(routes: Record<string, (c: Call) => Response | Promise<Response>>, role: 'admin' | 'member' = 'admin', path = '/connections') {
  const result = start(path, { 'GET /v1/whoami': whoami(role), 'GET /v1/connections': () => json(200, { connections: [LEDGER, CRM] }), ...routes }, signedIn());
  await screen.findByRole('heading', { level: 1 }, { timeout: FIRST_RENDER_MS });
  return result;
}

async function detail(name: 'ledger' | 'crm', routes: Record<string, (c: Call) => Response | Promise<Response>> = {}, role: 'admin' | 'member' = 'admin') {
  const id = name === 'ledger' ? LEDGER.id : CRM.id;
  const result = await connectionsPage(routes, role, `/connections/${id}`);
  await screen.findByRole('heading', { name, level: 1 });
  return result;
}

const panel = (name: string) => screen.getByRole('region', { name });

async function openAdd() {
  await screen.findByText('ledger');
  fireEvent.click(screen.getByRole('button', { name: 'Add connection' }));
  return screen.findByRole('dialog', { name: 'Add a connection' });
}

async function stepOne(dialog: HTMLElement) {
  fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: 'warehouse' } });
  fireEvent.change(within(dialog).getByLabelText('Owner: email or usr_ id'), { target: { value: OWNER } });
  fireEvent.change(within(dialog).getByLabelText('Host'), { target: { value: HOST } });
  fireEvent.change(within(dialog).getByLabelText('Database'), { target: { value: DATABASE } });
}

async function submitIn(dialog: HTMLElement, name: string) {
  await act(async () => {
    fireEvent.click(within(dialog).getByRole('button', { name }));
  });
}

async function change(region: string) {
  fireEvent.click(within(panel(region)).getByRole('button', { name: 'Change' }));
  return panel(region);
}

const GROUPS = () => json(200, { groups: [{ id: GROUP, name: 'Finance', member_count: 3 }] });
const OLU = () => json(200, { users: [{ id: OWNER_TWO, display_name: 'Olu', email: 'olu@example.com', role: 'member', status: 'active' }] });

async function find(region: HTMLElement, label: string | RegExp, text: string, pick: RegExp) {
  const input = within(region).getByLabelText(label);
  fireEvent.change(input, { target: { value: text } });
  const box = input.closest('.stack') as HTMLElement;
  await act(async () => {
    fireEvent.click(within(box).getByRole('button', { name: 'Find' }));
  });
  await within(region).findByRole('radio', { name: pick });
}

describe('connections page', () => {
  it('lists four columns by name, never an id or an address', async () => {
    await connectionsPage({});
    const table = await screen.findByRole('table', { name: 'Connections' });
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Name', 'Kind', 'State', 'Owner']);
    expect(within(table).getByRole('link', { name: 'crm' }).getAttribute('href')).toBe(`/connections/${CRM.id}`);
    expect(within(table).getAllByText('Olive Owner')).toHaveLength(2);
    expect(within(table).getByText('pending')).toBeTruthy();
    for (const id of [GROUP, OWNER, LEDGER.id, CRM.id]) expect(table.textContent).not.toContain(id);
    expect(document.body.textContent).not.toContain('host');
    expect(screen.queryByRole('searchbox')).toBeNull();
  });

  it('filters by name or kind once there are more than ten', async () => {
    const many = Array.from({ length: 11 }, (_, i) => connection(`db-${String.fromCharCode(97 + i)}`));
    await connectionsPage({ 'GET /v1/connections': () => json(200, { connections: [...many, connection('sheet', { kind: 'gsheets' })] }) });
    await screen.findByText('db-a');
    const box = screen.getByRole('searchbox', { name: 'Filter connections' });
    fireEvent.change(box, { target: { value: 'google' } });
    const table = screen.getByRole('table', { name: 'Connections' });
    expect(within(table).getAllByRole('row')).toHaveLength(2);
    expect(within(table).getByText('sheet')).toBeTruthy();
  });

  it('opens a row as its own page', async () => {
    const { router } = await connectionsPage({});
    const row = (await screen.findByRole('link', { name: 'crm' })).closest('tr') as HTMLElement;
    fireEvent.click(within(row).getByText('PostgreSQL'));
    await screen.findByRole('heading', { name: 'crm', level: 1 });
    expect(router.state.location.pathname).toBe(`/connections/${CRM.id}`);
  });

  it('shows the overview, schemas, limits and ceiling, and status of one connection', async () => {
    await detail('crm');
    for (const name of ['Overview', 'Schemas', 'Limits and ceiling', 'Status']) expect(panel(name)).toBeTruthy();
    const overview = panel('Overview');
    expect(overview.textContent).toContain('Olive Owner');
    expect(overview.textContent).toContain('confidential');
    expect(overview.textContent).toContain('Finance');
    expect(overview.textContent).toContain('Members of Finance');
    expect(within(overview).getByRole('button', { name: 'Copy connection id' })).toBeTruthy();
    expect(document.body.textContent?.split(CRM.id)).toHaveLength(2);
    expect(document.body.textContent).not.toContain(GROUP);
    expect(panel('Limits and ceiling').textContent).toContain('Rows per query 1000');
    expect(within(screen.getByRole('navigation', { name: 'Breadcrumb' })).getByText('crm')).toBeTruthy();
  });

  it('adds a connection with only the first step filled, on the defaults', async () => {
    const { api } = await connectionsPage({ 'POST /v1/connections': created });
    const dialog = await openAdd();
    expect((within(dialog).getByText('More options').closest('details') as HTMLDetailsElement).open).toBe(false);
    await stepOne(dialog);
    await submitIn(dialog, 'Add connection');
    expect(api.of('POST', '/v1/connections')[0]?.body).toEqual({
      name: 'warehouse',
      kind: 'postgres',
      owner_user_id: OWNER,
      classification: 'internal',
      ceiling: { audience: 'org' },
      allowed_schemas: ['public'],
      limits: {},
      preview_access: { who: 'builders' },
      host: HOST,
      port: 5432,
      database: DATABASE,
    });
    expect(await toast()).toContain('Added warehouse.');
  });

  it('adds a connection with more options, its address in the body only, then forgets the address', async () => {
    const { api, queryClient } = await connectionsPage({ 'GET /v1/users': OLU, 'GET /v1/groups': GROUPS, 'POST /v1/connections': created });
    const dialog = await openAdd();
    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: 'warehouse' } });
    fireEvent.change(within(dialog).getByLabelText('Classification'), { target: { value: 'restricted' } });
    await find(dialog, 'Owner: email or usr_ id', 'olu@example.com', /Olu/);
    fireEvent.change(within(dialog).getByLabelText('Host'), { target: { value: HOST } });
    fireEvent.change(within(dialog).getByLabelText('Port'), { target: { value: '6432' } });
    fireEvent.change(within(dialog).getByLabelText('Database'), { target: { value: DATABASE } });
    fireEvent.change(within(dialog).getByLabelText(/^Schemas/), { target: { value: 'public, finance' } });
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Only the groups and people listed' }));
    await find(dialog, /^Group name, email/, 'Finance', /Finance/);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add' }));
    expect(within(within(dialog).getByRole('list', { name: 'Groups and people' })).getByText('Finance')).toBeTruthy();
    fireEvent.change(within(dialog).getByLabelText('Rows per query'), { target: { value: '500' } });
    await submitIn(dialog, 'Add connection');
    const post = api.of('POST', '/v1/connections')[0];
    expect(post?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(post?.body).toEqual({
      name: 'warehouse',
      kind: 'postgres',
      owner_user_id: OWNER_TWO,
      classification: 'restricted',
      ceiling: { audience: 'subjects', subjects: [{ kind: 'group', id: GROUP }] },
      allowed_schemas: ['public', 'finance'],
      limits: { max_rows: 500 },
      preview_access: { who: 'approval' },
      host: HOST,
      port: 6432,
      database: DATABASE,
    });
    expect(await toast()).toContain('Added warehouse.');
    expect(screen.queryByRole('dialog')).toBeNull();
    for (const call of api.calls) expect(`${call.path}?${call.query.toString()}`).not.toContain(HOST);
    const cached = JSON.stringify(queryClient.getQueryCache().getAll().map((q) => [q.queryKey, q.state.data]));
    expect(cached).not.toContain(HOST);
    expect(cached).not.toContain(DATABASE);
    const again = await openAdd();
    expect((within(again).getByLabelText('Host') as HTMLInputElement).value).toBe('');
    expect((within(again).getByLabelText('Database') as HTMLInputElement).value).toBe('');
  });

  it('starts preview access from the classification until it is picked, and sends a group rule', async () => {
    const { api } = await connectionsPage({ 'POST /v1/connections': created });
    const dialog = await openAdd();
    const preview = within(dialog).getByLabelText('Preview access') as HTMLSelectElement;
    expect(preview.value).toBe('builders');
    fireEvent.change(within(dialog).getByLabelText('Classification'), { target: { value: 'confidential' } });
    expect(preview.value).toBe('approval');
    expect(within(dialog).queryByLabelText('Group name or grp_ id')).toBeNull();
    fireEvent.change(preview, { target: { value: 'group' } });
    fireEvent.change(within(dialog).getByLabelText('Classification'), { target: { value: 'internal' } });
    expect(preview.value).toBe('group');
    fireEvent.change(within(dialog).getByLabelText('Group name or grp_ id'), { target: { value: GROUP } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Use' }));
    await stepOne(dialog);
    await submitIn(dialog, 'Add connection');
    const body = api.of('POST', '/v1/connections')[0]?.body as Record<string, unknown>;
    expect(body.classification).toBe('internal');
    expect(body.preview_access).toEqual({ who: 'group', group_id: GROUP });
  });

  it('clears the address once sent even when the API refuses, and shows why', async () => {
    const { api } = await connectionsPage({
      'POST /v1/connections': () => problem(422, 'CEILING_REQUIRED', 'This connection needs an audience ceiling.'),
    });
    const dialog = await openAdd();
    await stepOne(dialog);
    await submitIn(dialog, 'Add connection');
    expect(api.of('POST', '/v1/connections')).toHaveLength(1);
    expect((await within(dialog).findByRole('alert')).textContent).toContain('CEILING_REQUIRED');
    expect((within(dialog).getByLabelText('Host') as HTMLInputElement).value).toBe('');
    expect((within(dialog).getByLabelText('Database') as HTMLInputElement).value).toBe('');
    expect(within(dialog).getByText(/enter it again to retry/)).toBeTruthy();
    expect((within(dialog).getByRole('button', { name: 'Add connection' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('says by the schemas field that the data gateway does not enforce them, when adding and changing', async () => {
    const hint = (root: HTMLElement) => {
      const field = within(root).getByLabelText(/^Schemas/);
      return document.getElementById(field.getAttribute('aria-describedby') ?? '')?.textContent ?? '';
    };
    await connectionsPage({});
    const adding = await openAdd();
    expect(hint(adding)).toContain('not enforced by the data gateway');
    fireEvent.click(within(adding).getByRole('button', { name: 'Cancel' }));
    fireEvent.click(screen.getByRole('link', { name: 'ledger' }));
    await screen.findByRole('heading', { name: 'ledger', level: 1 });
    expect(panel('Schemas').textContent).toContain('not enforced by the data gateway');
    expect(hint(await change('Schemas'))).toContain('not enforced by the data gateway');
  });

  it('changes the owner through the same lookup as adding', async () => {
    const { api } = await detail('ledger', {
      'GET /v1/users': OLU,
      'PATCH /v1/connections/ledger': ({ body }) => json(200, { ...LEDGER, ...(body as object) }),
    });
    const overview = await change('Overview');
    await find(overview, 'Owner: email or usr_ id', 'olu@example.com', /Olu/);
    await submitIn(overview, 'Save changes');
    expect(api.of('PATCH', '/v1/connections/ledger')[0]?.body).toEqual({ owner_user_id: OWNER_TWO });
    expect(await toast()).toBe('Changed ledger.');
    await waitFor(() => expect(within(panel('Overview')).queryByRole('button', { name: 'Save changes' })).toBeNull());
  });

  it('changes only what was edited, and sends the ceiling with a move to confidential', async () => {
    const { api } = await detail('ledger', {
      'PATCH /v1/connections/ledger': ({ body }) => json(200, { ...LEDGER, ...(body as object) }),
    });
    const overview = await change('Overview');
    fireEvent.change(within(overview).getByLabelText('Classification'), { target: { value: 'confidential' } });
    fireEvent.change(within(overview).getByLabelText('Setup'), { target: { value: 'pending' } });
    fireEvent.change(within(overview).getByLabelText('Preview access'), { target: { value: 'approval' } });
    await submitIn(overview, 'Save changes');
    expect(api.of('PATCH', '/v1/connections/ledger')[0]?.body).toEqual({
      classification: 'confidential',
      ceiling: { audience: 'org' },
      preview_access: { who: 'approval' },
      setup_status: 'pending',
    });
  });

  it('edits the ceiling as names, keeping ids underneath', async () => {
    const { api } = await detail('crm', {
      'GET /v1/users': OLU,
      'PATCH /v1/connections/crm': ({ body }) => json(200, { ...CRM, ...(body as object) }),
    });
    const limits = await change('Limits and ceiling');
    const picks = within(limits).getByRole('list', { name: 'Groups and people' });
    expect(picks.textContent).toContain('Finance');
    expect(limits.textContent).not.toContain(GROUP);
    await find(limits, /^Group name, email/, 'olu@example.com', /Olu/);
    fireEvent.click(within(limits).getByRole('button', { name: 'Add' }));
    fireEvent.click(within(limits).getByRole('button', { name: 'Remove Finance' }));
    await submitIn(limits, 'Save changes');
    expect(api.of('PATCH', '/v1/connections/crm')[0]?.body).toEqual({
      ceiling: { audience: 'subjects', subjects: [{ kind: 'user', id: OWNER_TWO }] },
    });
  });

  it('suspends inline, asking once for the name, and says apps lose it within seconds', async () => {
    const { api } = await detail('crm', {
      'PATCH /v1/connections/crm': ({ body }) => json(200, { ...CRM, ...(body as object) }),
    });
    const status = panel('Status');
    expect(status.textContent).toContain('Suspending stops every query on it, for every app');
    expect(within(status).queryByRole('button', { name: /Resume/ })).toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(within(status).getByRole('button', { name: 'Suspend this connection' }));
    const confirm = await screen.findByRole('dialog', { name: 'Suspend crm' });
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(confirm.textContent).toContain('Every app linked to crm loses it within a few seconds');
    const go = within(confirm).getByRole('button', { name: 'Suspend' }) as HTMLButtonElement;
    expect(go.disabled).toBe(true);
    fireEvent.change(within(confirm).getByLabelText(/to confirm/), { target: { value: 'ledger' } });
    expect(go.disabled).toBe(true);
    fireEvent.change(within(confirm).getByLabelText(/to confirm/), { target: { value: 'crm' } });
    await act(async () => {
      fireEvent.click(go);
    });
    const [call] = api.of('PATCH', '/v1/connections/crm');
    expect(call?.body).toEqual({ status: 'suspended' });
    expect(call?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(await toast()).toBe(
      'Suspended crm. Apps linked to it lose it within a few seconds: every query on it is refused until it is resumed.',
    );
    await waitFor(() => expect(api.of('GET', '/v1/connections').length).toBeGreaterThan(1));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('shows a refused suspension in the confirmation and keeps it open', async () => {
    await detail('crm', { 'PATCH /v1/connections/crm': () => problem(403, 'AGENT_SESSION_REFUSED', 'Not in an agent session') });
    fireEvent.click(within(panel('Status')).getByRole('button', { name: 'Suspend this connection' }));
    const confirm = await screen.findByRole('dialog', { name: 'Suspend crm' });
    fireEvent.change(within(confirm).getByLabelText(/to confirm/), { target: { value: 'crm' } });
    await submitIn(confirm, 'Suspend');
    expect((await within(confirm).findByRole('alert')).textContent).toContain('AGENT_SESSION_REFUSED');
  });

  it('resumes a suspended connection after one confirming click', async () => {
    const suspended = { ...CRM, status: 'suspended' };
    const { api } = await detail('crm', {
      'GET /v1/connections': () => json(200, { connections: [LEDGER, suspended] }),
      'PATCH /v1/connections/crm': ({ body }) => json(200, { ...suspended, ...(body as object) }),
    });
    const status = panel('Status');
    expect(status.textContent).toContain('Every query on it is refused.');
    expect(within(status).queryByRole('button', { name: /Suspend/ })).toBeNull();
    fireEvent.click(within(status).getByRole('button', { name: 'Resume this connection' }));
    const confirm = await screen.findByRole('dialog', { name: 'Resume crm' });
    expect(within(confirm).queryByLabelText(/to confirm/)).toBeNull();
    await submitIn(confirm, 'Resume');
    expect(api.of('PATCH', '/v1/connections/crm').map((c) => c.body)).toEqual([{ status: 'active' }]);
    expect(await toast()).toBe('Resumed crm. Apps linked to it can query it again within a few seconds.');
  });

  it('sends nothing when nothing changed', async () => {
    const { api } = await detail('crm');
    for (const name of ['Overview', 'Schemas', 'Limits and ceiling']) {
      await submitIn(await change(name), 'Save changes');
    }
    expect(api.of('PATCH', '/v1/connections/crm')).toHaveLength(0);
  });

  it('shows a member the list and the page without any way to change them', async () => {
    await connectionsPage({}, 'member');
    await screen.findByText('ledger');
    expect(screen.getByText(/Only org admins can add or change connections/)).toBeTruthy();
    expect(screen.getByText(/You see every connection of the org, never its address/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Add connection' })).toBeNull();
    fireEvent.click(screen.getByRole('link', { name: 'crm' }));
    await screen.findByRole('heading', { name: 'crm', level: 1 });
    expect(screen.queryByRole('button', { name: 'Change' })).toBeNull();
    expect(screen.queryByRole('button', { name: /Suspend|Resume/ })).toBeNull();
  });
});

describe("an environment's data connections", () => {
  it('reads nothing until the section is opened', async () => {
    const { api } = start(`/apps/${'app_aaaaaaaaaaaaaaaaaaaa'}`, page({ [`GET ${ENV_CONNECTIONS}`]: () => json(200, linked(LEDGER)) }), signedIn());
    const panel = await envPanel('Production');
    await within(panel).findByText('Everyone in the organisation');
    expect(api.of('GET', ENV_CONNECTIONS)).toHaveLength(0);
    const section = await openSection(panel, 'Data connections');
    expect(await within(section).findByText('ledger')).toBeTruthy();
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
    const panel = await openSection(await envPanel('Production'), 'Data connections');
    const select = (await within(panel).findByLabelText('Connection to link')) as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual(['', 'crm']);
    fireEvent.change(select, { target: { value: 'crm' } });
    await act(async () => {
      fireEvent.click(within(panel).getByRole('button', { name: 'Link connection' }));
    });
    expect(api.of('PUT', `${ENV_CONNECTIONS}/crm`)[0]?.body).toEqual({});
    expect(await toast()).toBe('Linked crm.');
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
    const panel = await openSection(await envPanel('Production'), 'Data connections');
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
    const link = await within(panel).findByRole('link', { name: 'open the request' });
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
    const panel = await openSection(await envPanel('Production'), 'Data connections');
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
    const panel = await openSection(await envPanel('Production'), 'Data connections');
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
    const panel = await openSection(await envPanel('Production'), 'Data connections');
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
    const panel = await openSection(await envPanel('Production'), 'Data connections');
    const row = (await within(panel).findByText('crm')).closest('tr');
    if (!row) throw new Error('no row');
    await openSection(row, 'Columns');
    expect((await within(panel).findByRole('alert')).textContent).toContain('CONNECTION_NOT_GRANTED');
  });
});
