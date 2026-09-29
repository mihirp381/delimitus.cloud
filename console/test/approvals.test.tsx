import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { approvalsSearch, requestedGrants } from '../src/api/approvals';
import { type Handler, json, problem } from './fakeApi';
import { signedIn, start } from './harness';

const OWNER = 'usr_cccccccccccccccccccc';
const ADMIN = 'usr_dddddddddddddddddddd';
const APP = {
  id: 'app_aaaaaaaaaaaaaaaaaaaa',
  slug: 'expenses',
  owner_user_id: OWNER,
  status: 'active',
  created_at: '2026-09-28T10:00:00Z',
  environments: [
    { id: 'env_preview0000000000000', name: 'preview', config_version: 1, grants_version: 0, current_deployment_id: null },
    { id: 'env_prod0000000000000000', name: 'prod', config_version: 2, grants_version: 3, current_deployment_id: null },
  ],
};
const GONE_APP = 'app_gggggggggggggggggggg';

function approval(id: string, overrides: Record<string, unknown>) {
  return {
    id,
    app_id: APP.id,
    environment_id: 'env_prod0000000000000000',
    kind: 'connect_data_source',
    subject_key: 'warehouse',
    payload: {},
    state: 'pending',
    requested_by_user_id: OWNER,
    requested_via_agent: false,
    decided_by_user_id: null,
    decided_at: null,
    decision_reason: null,
    decision_channel: null,
    recorded_by_operator: null,
    policy_decision_id: null,
    created_at: '2026-09-29T09:00:00Z',
    ...overrides,
  };
}

const WIDEN = approval('apr_000000000000000000w1', {
  kind: 'widen_audience',
  subject_key: `sha256:${'a'.repeat(64)}`,
  payload: { grants: [{ role: 'user', subject_kind: 'org' }] },
});
const AGENT_SHARE = approval('apr_000000000000000000s1', {
  kind: 'agent_share',
  subject_key: `sha256:${'b'.repeat(64)}`,
  payload: { grants_version: 3, grants: [{ role: 'builder', subject_kind: 'user', subject_id: OWNER }] },
  state: 'approved',
  requested_via_agent: true,
  decided_by_user_id: ADMIN,
  decided_at: '2026-09-29T11:00:00Z',
  decision_reason: 'Fine for the pilot',
  decision_channel: 'email',
  recorded_by_operator: 'op_ada',
  policy_decision_id: 'pdc_cccccccccccccccccccc',
});
const DENIED = approval('apr_000000000000000000d1', {
  state: 'denied',
  decided_by_user_id: ADMIN,
  decided_at: '2026-09-29T12:00:00Z',
  decision_reason: 'Not this quarter',
  decision_channel: 'chat',
  recorded_by_operator: 'op_ada',
  environment_id: 'env_preview0000000000000',
});
const CANCELLED = approval('apr_000000000000000000c1', {
  app_id: GONE_APP,
  environment_id: 'env_gone0000000000000000',
  kind: 'enable_internet_hosts',
  subject_key: 'api.example.com',
  state: 'cancelled',
});

function page(approvals: unknown[], next: string | null = null): Handler {
  return () => json(200, { approvals, next_before: next });
}

function routes(list: Handler | Handler[]): Record<string, Handler | Handler[]> {
  return {
    'GET /v1/approvals': list,
    [`GET /v1/apps/${APP.id}`]: () => json(200, APP),
    [`GET /v1/apps/${GONE_APP}`]: () => problem(404, 'NOT_FOUND', 'Not found'),
  };
}

function rowOf(text: string): HTMLElement {
  const row = screen.getByText(text).closest('tr');
  if (!row) throw new Error(`no row holds ${text}`);
  return row;
}

describe('approvals', () => {
  it('lists each request with what it asks, who asked and how it was decided', async () => {
    const { api } = start('/approvals', routes(page([WIDEN, AGENT_SHARE, DENIED, CANCELLED])), signedIn());
    expect(await screen.findByText('4 requests')).toBeTruthy();

    const widen = rowOf('Widen who has access, to:');
    expect(within(widen).getByText('Everyone in the organisation (user)')).toBeTruthy();
    expect(within(widen).getByText('pending')).toBeTruthy();
    expect(within(widen).getByText('Waiting for an org admin')).toBeTruthy();
    expect(within(widen).queryByText('agent')).toBeNull();
    await within(widen).findByRole('link', { name: 'expenses' });
    expect(within(widen).getByText('prod')).toBeTruthy();

    const share = rowOf('Sharing change made by an agent, replacing version 3, to:');
    expect(within(share).getByText(`User ${OWNER} (builder)`)).toBeTruthy();
    expect(within(share).getByText('agent')).toBeTruthy();
    expect(within(share).getByText('approved')).toBeTruthy();
    expect(within(share).getByText(ADMIN)).toBeTruthy();
    expect(within(share).getByText(/by email on/)).toBeTruthy();
    expect(within(share).getByText('“Fine for the pilot”')).toBeTruthy();
    expect(within(share).getByText('op_ada')).toBeTruthy();

    const denied = rowOf('warehouse');
    expect(within(denied).getByText(/Connect the data source/)).toBeTruthy();
    expect(within(denied).getByText('denied')).toBeTruthy();
    expect(within(denied).getByText(/by chat on/)).toBeTruthy();
    expect(within(denied).getByText('preview')).toBeTruthy();

    // An app the caller cannot read shows its ids.
    const cancelled = rowOf('api.example.com');
    expect(within(cancelled).getByText('Withdrawn by the requester')).toBeTruthy();
    await within(cancelled).findByText('env_gone0000000000000000');
    expect(within(cancelled).getByRole('link', { name: GONE_APP }).getAttribute('href')).toBe(`/apps/${GONE_APP}`);

    // One read per app, however many rows name it.
    expect(api.of('GET', `/v1/apps/${APP.id}`)).toHaveLength(1);
    expect([...api.of('GET', '/v1/approvals')[0]!.query.keys()]).toEqual([]);
  });

  it('offers no way to approve or deny and sends nothing but reads', async () => {
    const { api } = start('/approvals', routes(page([WIDEN, DENIED], 'apr_000000000000000000d1')), signedIn());
    await screen.findByText('2 requests, more are older');
    const names = screen.getAllByRole('button').map((b) => b.textContent ?? '');
    expect(names).toEqual(['Sign out', 'Load older requests']);
    expect(names.filter((n) => /approve|deny|decide|reject/i.test(n))).toEqual([]);
    expect(api.calls.filter((c) => c.method !== 'GET')).toEqual([]);
  });

  it('filters by state through the URL', async () => {
    const { api, router } = start(
      '/approvals',
      routes((call) => (call.query.get('state') === 'pending' ? page([])(call) : page([WIDEN, DENIED])(call))),
      signedIn(),
    );
    await screen.findByText('2 requests');
    fireEvent.change(screen.getByLabelText('State'), { target: { value: 'pending' } });
    expect(await screen.findByText('No pending requests.')).toBeTruthy();
    expect(router.state.location.search).toEqual({ state: 'pending' });
    expect(api.of('GET', '/v1/approvals')[1]?.query.get('state')).toBe('pending');
    fireEvent.change(screen.getByLabelText('State'), { target: { value: '' } });
    await screen.findByText('2 requests');
    expect(router.state.location.search).toEqual({});
  });

  it('drops search values from the URL that the API would refuse', async () => {
    const { api } = start('/approvals?state=open&limit=5&before=apr_x', routes(page([WIDEN])), signedIn());
    await screen.findByText('1 request');
    expect([...api.of('GET', '/v1/approvals')[0]!.query.keys()]).toEqual([]);
  });

  it('loads older requests with the cursor the API returned', async () => {
    const { api } = start(
      '/approvals?state=denied',
      routes([page([DENIED], 'apr_000000000000000000d1'), page([CANCELLED])]),
      signedIn(),
    );
    await screen.findByText('1 request, more are older');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Load older requests' }));
    });
    expect(await screen.findByText('2 requests')).toBeTruthy();
    const second = api.of('GET', '/v1/approvals')[1]!.query;
    expect([...second.entries()].sort()).toEqual([
      ['before', 'apr_000000000000000000d1'],
      ['state', 'denied'],
    ]);
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Load older requests' })).toBeNull());
  });

  it('shows the refusal and its request id', async () => {
    start('/approvals', routes(() => problem(403, 'FORBIDDEN', 'Workload credentials see none')), signedIn());
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Workload credentials see none');
    expect(alert.textContent).toContain('req_test_0001');
  });
});

describe('approval helpers', () => {
  it('keeps only a known state from the URL', () => {
    expect(approvalsSearch({ state: 'approved' })).toEqual({ state: 'approved' });
    expect(approvalsSearch({ state: 'constructor' })).toEqual({});
    expect(approvalsSearch({ state: 'open' })).toEqual({});
  });

  it('reads the requested grants only when every one is well formed', () => {
    expect(requestedGrants({ grants: [{ role: 'user', subject_kind: 'org' }] })).toEqual([
      { role: 'user', subject_kind: 'org', subject_id: null },
    ]);
    expect(requestedGrants({ grants: [{ role: 'owner', subject_kind: 'org' }] })).toBeNull();
    expect(requestedGrants({ grants: 'all' })).toBeNull();
    expect(requestedGrants({})).toBeNull();
  });
});
