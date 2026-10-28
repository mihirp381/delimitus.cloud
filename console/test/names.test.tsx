import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { APP_ID, APP_PATH, FIRST_RENDER_MS, OWNER, openTab, page, PROD_GRANTS, whoami } from './appPage';
import { type Handler, json } from './fakeApi';
import { signedIn, start } from './harness';

const RAW_ID = /^(usr|app|apr|grp|con|org)_[a-z0-9]+$/;
const GROUP = 'grp_ffffffffffffffffffff';
const CONNECTION = 'con_llllllllllllllllllll';
const OLIVE = { name: 'Olive Owner', email: 'olive@example.com' };

function rawCells(): string[] {
  return [...document.querySelectorAll('td')].map((td) => (td.textContent ?? '').trim()).filter((t) => RAW_ID.test(t));
}

async function rows(name: string | RegExp): Promise<void> {
  const table = await screen.findByRole('table', { name }, { timeout: FIRST_RENDER_MS });
  await waitFor(() => expect(within(table).getAllByRole('row').length).toBeGreaterThan(1));
}

const connection = {
  id: CONNECTION,
  name: 'ledger',
  kind: 'postgres',
  owner_user_id: OWNER,
  owner_name: OLIVE.name,
  owner_email: OLIVE.email,
  classification: 'internal',
  ceiling: { audience: 'org', subjects: [] },
  allowed_schemas: ['public'],
  limits: { max_rows: 1000 },
  setup_status: 'ready',
  status: 'active',
  preview_access: { who: 'group', group_id: GROUP },
  preview_access_group_name: 'Finance',
  ceiling_subject_names: {},
  created_at: '2026-10-01T10:00:00Z',
  updated_at: '2026-10-01T10:00:00Z',
};

function audit(seq: number, actor: Record<string, unknown>, target: Record<string, unknown>) {
  return {
    seq,
    at: '2026-09-29T10:00:00Z',
    action: 'app.created',
    actor: { via_agent: false, client_id: null, email: null, ...actor },
    target,
    before: null,
    after: null,
    policy_decision_id: null,
    prev_hash: '0'.repeat(64),
    hash: seq.toString(16).padStart(64, '0'),
  };
}

const approval = {
  id: 'apr_aaaaaaaaaaaaaaaaaaaa',
  app_id: APP_ID,
  environment_id: 'env_prod0000000000000000',
  kind: 'agent_share',
  subject_key: `sha256:${'b'.repeat(64)}`,
  payload: { grants_version: 3, grants: [{ role: 'builder', subject_kind: 'user', subject_id: OWNER }, { role: 'user', subject_kind: 'group', subject_id: GROUP }] },
  state: 'approved',
  requested_by_user_id: OWNER,
  requested_by_name: OLIVE.name,
  requested_by_email: OLIVE.email,
  app: 'expenses',
  environment: 'prod',
  requested_via_agent: true,
  decided_by_user_id: 'usr_dddddddddddddddddddd',
  decided_by_name: 'Ada Admin',
  decided_by_email: 'ada@example.com',
  subject_names: { [OWNER]: OLIVE.name, [GROUP]: 'Finance' },
  decided_at: '2026-09-29T11:00:00Z',
  decision_reason: null,
  decision_channel: null,
  recorded_by_operator: null,
  policy_decision_id: null,
  created_at: '2026-09-29T09:00:00Z',
};

const LISTS: readonly [string, string, string | RegExp, Record<string, Handler>][] = [
  [
    'the apps you can open',
    '/',
    'Apps you can open',
    {
      'GET /v1/whoami': whoami('member'),
      'GET /v1/me/apps': () =>
        json(200, {
          user_id: OWNER,
          user_name: OLIVE.name,
          user_status: 'active',
          published_version: null,
          apps: [
            {
              app_id: APP_ID,
              slug: 'expenses',
              status: 'active',
              owner_name: OLIVE.name,
              owner_email: OLIVE.email,
              environments: [
                {
                  environment_id: 'env_prod0000000000000000',
                  name: 'prod',
                  url: 'https://expenses.acme.apps.example',
                  role: 'user',
                  via: [{ kind: 'group', name: 'Finance' }],
                },
              ],
            },
          ],
        }),
    },
  ],
  [
    'the inventory',
    '/',
    'Apps in this organisation',
    {
      'GET /v1/whoami': whoami('admin'),
      'GET /v1/usage': () => json(200, { month: '2026-10', fixed_resources: [], environments: [] }),
      'GET /v1/inventory': () =>
        json(200, {
          items: [
            {
              app_id: APP_ID,
              slug: 'expenses',
              status: 'active',
              created_at: '2026-09-28T10:00:00Z',
              owner: { user_id: OWNER, display_name: OLIVE.name, ...OLIVE },
              last_used_at: null,
              environments: [],
            },
          ],
          next_cursor: null,
        }),
    },
  ],
  ['connections', '/connections', 'Connections', { 'GET /v1/whoami': whoami('admin'), 'GET /v1/connections': () => json(200, { connections: [connection] }) }],
  [
    'the audit log',
    '/audit',
    /Audit events/,
    {
      'GET /v1/whoami': whoami('admin'),
      'GET /v1/audit': () =>
        json(200, {
          events: [
            audit(4, { kind: 'user', id: OWNER, ...OLIVE }, { kind: 'app', id: APP_ID, name: 'expenses' }),
            audit(3, { kind: 'operator', id: 'op_1', name: 'Delimitus operator' }, { kind: 'org', id: 'org_ffffffffffffffffffff', name: 'Acme' }),
            audit(2, { kind: 'workload', id: 'env_x', name: 'System' }, { kind: 'user_group', id: GROUP, name: 'Finance' }),
            audit(1, { kind: 'user', id: 'usr_gggggggggggggggggggg', name: 'Former member' }, { kind: 'connection', id: CONNECTION, name: 'ledger' }),
          ],
          next_before_seq: null,
        }),
    },
  ],
  [
    'internet access',
    '/egress',
    'Hosts apps may reach',
    {
      'GET /v1/whoami': whoami('admin'),
      'GET /v1/egress/catalogue': () => json(200, { entries: [] }),
      'GET /v1/egress': () =>
        json(200, {
          hosts: [
            { host: 'api.stripe.com', high_risk: false, added_by_user_id: OWNER, added_by_name: OLIVE.name, added_by_email: OLIVE.email, approval_request_id: null, created_at: '2026-10-01T10:00:00Z' },
            { host: 'api.twilio.com', high_risk: false, added_by_user_id: OWNER, added_by_name: OLIVE.name, added_by_email: OLIVE.email, approval_request_id: 'apr_aaaaaaaaaaaaaaaaaaaa', created_at: '2026-10-01T10:00:00Z' },
          ],
          outbound_ip: '34.120.10.20',
          proxy_address: null,
        }),
    },
  ],
  ['approvals', '/approvals?view=all', /requests/i, { 'GET /v1/whoami': whoami('admin'), 'GET /v1/approvals': () => json(200, { approvals: [approval], next_before: null }) }],
  [
    'the people roster',
    '/people',
    'People, by name',
    {
      'GET /v1/whoami': whoami('admin'),
      'GET /v1/unlinked-logins': () => json(200, { unlinked_logins: [] }),
      'GET /v1/users': () =>
        json(200, {
          users: [
            { id: OWNER, display_name: OLIVE.name, email: OLIVE.email, role: 'member', status: 'active', created_at: '2026-09-01T09:00:00Z', last_sign_in_at: null },
            { id: 'usr_gggggggggggggggggggg', display_name: '', email: 'nameless@example.com', role: 'admin', status: 'deactivated', created_at: '2026-09-01T09:00:00Z', last_sign_in_at: '2026-10-01T09:00:00Z' },
          ],
          next_cursor: null,
        }),
    },
  ],
  [
    'unmatched logins',
    '/people',
    /Unmatched logins/,
    {
      'GET /v1/whoami': whoami('admin'),
      'GET /v1/users': () => json(200, { users: [], next_cursor: null }),
      'GET /v1/unlinked-logins': () =>
        json(200, {
          unlinked_logins: [
            { id: 'ulg_pppppppppppppppppppp', connection_id: 'conn_01', subject: 's', email: 'pat@example.com', reason: 'no_match', attempts: 1, first_seen_at: '2026-10-01T09:00:00Z', last_seen_at: '2026-10-01T09:00:00Z', linkable: true },
          ],
        }),
    },
  ],
];

describe('lists show names, not ids', () => {
  it.each(LISTS)('%s', async (_, path, table, routes) => {
    start(path, routes, signedIn());
    await rows(table);
    expect(rawCells()).toEqual([]);
  });

  it("an app's access", async () => {
    const grants = [
      { id: 'gnt_000000000000000000b1', role: 'builder', subject_kind: 'user', subject_id: OWNER, subject_name: OLIVE.name, subject_email: OLIVE.email },
      { id: 'gnt_000000000000000000g1', role: 'user', subject_kind: 'group', subject_id: GROUP, subject_name: 'Finance', subject_email: null },
    ];
    start(
      `${APP_PATH.replace('/v1', '')}?tab=access`,
      page({ [`GET ${PROD_GRANTS}`]: () => json(200, { environment_id: 'env_prod0000000000000000', grants_version: 3, grants }) }),
      signedIn(),
    );
    await openTab('Access');
    const access = await screen.findByRole('region', { name: 'Who has access' }, { timeout: FIRST_RENDER_MS });
    await within(access).findByText('Finance');
    expect(within(access).getByText(OLIVE.name)).toBeTruthy();
    expect(rawCells()).toEqual([]);
  });
});
