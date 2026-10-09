import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { approvalsSearch, requestedGrants } from '../src/api/approvals';
import { type Handler, json, problem } from './fakeApi';
import { signedIn, start } from './harness';

const OWNER = 'usr_cccccccccccccccccccc';
const ADMIN = 'usr_dddddddddddddddddddd';
const APP_ID = 'app_aaaaaaaaaaaaaaaaaaaa';
const GONE_APP = 'app_gggggggggggggggggggg';

function approval(id: string, overrides: Record<string, unknown>) {
  return {
    id,
    app_id: APP_ID,
    environment_id: 'env_prod0000000000000000',
    kind: 'connect_data_source',
    subject_key: 'warehouse',
    payload: {},
    state: 'pending',
    requested_by_user_id: OWNER,
    requested_by_name: 'Olivia Owner',
    app: 'expenses',
    environment: 'prod',
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
  environment: 'preview',
});
const CANCELLED = approval('apr_000000000000000000c1', {
  app_id: GONE_APP,
  app: 'old-app',
  environment_id: 'env_gone0000000000000000',
  environment: 'preview',
  kind: 'enable_internet_hosts',
  subject_key: 'api.example.com',
  state: 'cancelled',
});

function page(approvals: unknown[], next: string | null = null): Handler {
  return () => json(200, { approvals, next_before: next });
}

function routes(list: Handler | Handler[]): Record<string, Handler | Handler[]> {
  return { 'GET /v1/approvals': list };
}

function rowOf(text: string): HTMLElement {
  const row = screen.getByText(text).closest('tr');
  if (!row) throw new Error(`no row holds ${text}`);
  return row;
}

function detail(base: Record<string, unknown>, extra: Record<string, unknown> = {}) {
  return {
    ...base,
    grant_diff: null,
    connection: null,
    can_decide: false,
    can_cancel: false,
    ...extra,
  };
}

const EXCEED = approval('apr_000000000000000000e1', {
  kind: 'exceed_ceiling',
  subject_key: 'sha256:cc',
  payload: { connection: 'warehouse', grants: [{ role: 'user', subject_kind: 'org' }] },
  requested_by_name: 'Bea Builder',
});
const DIFF = {
  added: [
    { role: 'user', subject_kind: 'org', subject_id: null, subject_name: null },
    { role: 'user', subject_kind: 'group', subject_id: 'grp_a', subject_name: 'Finance' },
  ],
  removed: [{ role: 'user', subject_kind: 'user', subject_id: ADMIN, subject_name: 'Ada Admin' }],
};
const CONNECTION = {
  name: 'warehouse',
  classification: 'confidential',
  owner_user_id: OWNER,
  ceiling_audience: 'subjects',
  ceiling_subjects: 2,
};

describe('approvals inbox', () => {
  it('asks the API for the inbox and lists what is waiting with names', async () => {
    const { api } = start('/approvals', routes(page([EXCEED, WIDEN])), signedIn());
    expect(await screen.findByText('2 requests waiting for you')).toBeTruthy();
    expect([...api.of('GET', '/v1/approvals')[0]!.query.entries()]).toEqual([['inbox', 'true']]);

    const exceed = rowOf('Share beyond the audience ceiling of the connection');
    expect(within(exceed).getByText('warehouse')).toBeTruthy();
    expect(within(exceed).getByText(/Bea Builder/)).toBeTruthy();
    expect(within(exceed).getByRole('link', { name: 'expenses' }).getAttribute('href')).toBe(`/apps/${APP_ID}`);
    expect(within(exceed).getByText('prod')).toBeTruthy();
    expect(within(exceed).getByRole('link', { name: 'Open' }).getAttribute('href')).toBe(
      '/approvals/apr_000000000000000000e1',
    );
    expect(api.calls.filter((c) => c.method !== 'GET')).toEqual([]);
  });

  it('says so when nothing is waiting', async () => {
    start('/approvals', routes(page([])), signedIn());
    expect(await screen.findByText('Nothing is waiting for you.')).toBeTruthy();
  });

  it('lists every request, with its decision, in the all view', async () => {
    const { api } = start('/approvals?view=all', routes(page([WIDEN, AGENT_SHARE, DENIED, CANCELLED])), signedIn());
    expect(await screen.findByText('4 requests')).toBeTruthy();
    expect([...api.of('GET', '/v1/approvals')[0]!.query.keys()]).toEqual([]);

    const widen = rowOf('Widen who has access, to:');
    expect(within(widen).getByText('Everyone in the organisation (user)')).toBeTruthy();
    expect(within(widen).getByText('pending')).toBeTruthy();
    expect(within(widen).getByText('Waiting for an approver')).toBeTruthy();
    expect(within(widen).queryByText('agent')).toBeNull();

    const share = rowOf('Sharing change made by an agent, replacing version 3, to:');
    expect(within(share).getByText('agent')).toBeTruthy();
    expect(within(share).getByText('approved')).toBeTruthy();
    expect(within(share).getByText(ADMIN)).toBeTruthy();
    expect(within(share).getByText(/by email on/)).toBeTruthy();
    expect(within(share).getByText('“Fine for the pilot”')).toBeTruthy();
    expect(within(share).getByText('op_ada')).toBeTruthy();

    const denied = rowOf('warehouse');
    expect(within(denied).getByText('denied')).toBeTruthy();
    expect(within(denied).getByText(/by chat on/)).toBeTruthy();

    const cancelled = rowOf('api.example.com');
    expect(within(cancelled).getByText('Withdrawn by the requester')).toBeTruthy();
    expect(within(cancelled).getByRole('link', { name: 'old-app' }).getAttribute('href')).toBe(`/apps/${GONE_APP}`);
  });

  it('switches views and filters by state through the URL', async () => {
    const { api, router } = start(
      '/approvals?view=all',
      routes((call) => (call.query.get('state') === 'pending' ? page([])(call) : page([WIDEN, DENIED])(call))),
      signedIn(),
    );
    await screen.findByText('2 requests');
    fireEvent.change(screen.getByLabelText('State'), { target: { value: 'pending' } });
    expect(await screen.findByText('No pending requests.')).toBeTruthy();
    expect(router.state.location.search).toEqual({ view: 'all', state: 'pending' });
    expect(api.of('GET', '/v1/approvals')[1]?.query.get('state')).toBe('pending');
    fireEvent.click(screen.getByRole('link', { name: 'Waiting for you' }));
    await screen.findByText('2 requests waiting for you');
    expect(router.state.location.search).toEqual({});
  });

  it('drops search values from the URL that the API would refuse', async () => {
    const { api } = start('/approvals?view=mine&state=open&limit=5&before=apr_x', routes(page([WIDEN])), signedIn());
    await screen.findByText('1 request waiting for you');
    expect([...api.of('GET', '/v1/approvals')[0]!.query.entries()]).toEqual([['inbox', 'true']]);
  });

  it('loads older requests with the cursor the API returned', async () => {
    const { api } = start(
      '/approvals?view=all&state=denied',
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

describe('approval detail', () => {
  const path = `/approvals/${EXCEED.id}`;
  const getRoute = (body: unknown) => ({ [`GET /v1/approvals/${EXCEED.id}`]: () => json(200, body) });

  it('shows the diff, who asked and the connection it goes beyond', async () => {
    start(path, getRoute(detail(EXCEED, { grant_diff: DIFF, connection: CONNECTION, can_decide: true })), signedIn());
    expect(await screen.findByText('Change to who has access')).toBeTruthy();
    expect(screen.getByText(/Bea Builder/)).toBeTruthy();
    const added = screen.getByRole('heading', { name: 'Added' }).nextElementSibling as HTMLElement;
    expect(within(added).getByText('Everyone in the organisation (user)')).toBeTruthy();
    expect(within(added).getByText('Group Finance (user)')).toBeTruthy();
    const removed = screen.getByRole('heading', { name: 'Removed' }).nextElementSibling as HTMLElement;
    expect(within(removed).getByText('User Ada Admin (user)')).toBeTruthy();
    expect(screen.getByText(/ceiling subjects, 2 named/)).toBeTruthy();
  });

  it('requires a reason, sends the decision and says the grant is in force', async () => {
    const { api } = start(
      path,
      {
        ...getRoute(detail(EXCEED, { grant_diff: DIFF, connection: CONNECTION, can_decide: true })),
        [`POST /v1/approvals/${EXCEED.id}/decide`]: () =>
          json(200, { ...EXCEED, state: 'approved', applied: 'applied', applied_reason: null }),
      },
      signedIn(),
    );
    const approve = await screen.findByRole('button', { name: 'Approve' });
    expect((approve as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Reject' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: '   ' } });
    expect((approve as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Fine for finance' } });
    await act(async () => {
      fireEvent.click(approve);
    });
    expect(await screen.findByText('Approved, and the sharing change is now in force.')).toBeTruthy();
    expect(api.of('POST', `/v1/approvals/${EXCEED.id}/decide`)[0]!.body).toEqual({
      outcome: 'approved',
      reason: 'Fine for finance',
      channel: 'console',
    });
  });

  it('says when an approval was not applied', async () => {
    start(
      path,
      {
        ...getRoute(detail(EXCEED, { can_decide: true })),
        [`POST /v1/approvals/${EXCEED.id}/decide`]: () =>
          json(200, { ...EXCEED, state: 'approved', applied: 'not_applied', applied_reason: 'stale' }),
      },
      signedIn(),
    );
    fireEvent.change(await screen.findByLabelText('Reason'), { target: { value: 'ok' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    });
    expect(
      await screen.findByText(/Approved, but the change was not applied \(the sharing rules changed after it was asked\)\. Ask again\./),
    ).toBeTruthy();
  });

  it('rejects with the reason', async () => {
    const { api } = start(
      path,
      {
        ...getRoute(detail(EXCEED, { can_decide: true })),
        [`POST /v1/approvals/${EXCEED.id}/decide`]: () => json(200, { ...EXCEED, state: 'denied', applied: 'not_applicable', applied_reason: null }),
      },
      signedIn(),
    );
    fireEvent.change(await screen.findByLabelText('Reason'), { target: { value: 'Too broad' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
    });
    expect(await screen.findByText('Rejected. The requester has been told why.')).toBeTruthy();
    expect(api.of('POST', `/v1/approvals/${EXCEED.id}/decide`)[0]!.body).toEqual({
      outcome: 'denied',
      reason: 'Too broad',
      channel: 'console',
    });
  });

  it('shows the refusal when the decision is refused', async () => {
    start(
      path,
      {
        ...getRoute(detail(EXCEED, { can_decide: true })),
        [`POST /v1/approvals/${EXCEED.id}/decide`]: () => problem(403, 'AGENT_SESSION_REFUSED', 'An agent session cannot approve'),
      },
      signedIn(),
    );
    fireEvent.change(await screen.findByLabelText('Reason'), { target: { value: 'ok' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    });
    expect((await screen.findByRole('alert')).textContent).toContain('An agent session cannot approve');
  });

  it('offers no decision to the requester, only to withdraw', async () => {
    const { api } = start(
      path,
      {
        ...getRoute(detail(EXCEED, { can_cancel: true })),
        [`POST /v1/approvals/${EXCEED.id}/cancel`]: () => json(200, { ...EXCEED, state: 'cancelled' }),
      },
      signedIn(),
    );
    await screen.findByRole('button', { name: 'Withdraw this request' });
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
    expect(screen.queryByLabelText('Reason')).toBeNull();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Withdraw this request' }));
    });
    expect(await screen.findByText('The request was withdrawn.')).toBeTruthy();
    expect(api.of('POST', `/v1/approvals/${EXCEED.id}/cancel`)[0]!.body).toEqual({
      reason: 'Withdrawn by the requester.',
      channel: 'console',
    });
  });

  it('shows a rejection with its reason and no buttons', async () => {
    start(path, getRoute(detail(DENIED, { id: EXCEED.id })), signedIn());
    expect(await screen.findByText('“Not this quarter”')).toBeTruthy();
    expect(screen.getByText(/Rejected by/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /approve|reject|withdraw/i })).toBeNull();
  });

  it('explains a data source request changes nothing by itself', async () => {
    start(path, getRoute(detail(approval(EXCEED.id, { can_decide: true }), { can_decide: true })), signedIn());
    expect(await screen.findByText(/changes nothing by itself/)).toBeTruthy();
  });

  it('says approving an internet host adds it to the allowlist', async () => {
    const asked = approval(EXCEED.id, { kind: 'enable_internet_hosts', subject_key: 'api.twilio.com', payload: {} });
    start(path, getRoute(detail(asked, { can_decide: true })), signedIn());
    const text = await screen.findByText(/to your org's allowlist/);
    expect(text.textContent).toBe("Allow the internet host api.twilio.com. Approving adds api.twilio.com to your org's allowlist.");
    expect(screen.queryByText(/changes nothing by itself/)).toBeNull();
  });
});

describe('approval helpers', () => {
  it('keeps only a known state and view from the URL', () => {
    expect(approvalsSearch({ state: 'approved' })).toEqual({ state: 'approved' });
    expect(approvalsSearch({ state: 'constructor' })).toEqual({});
    expect(approvalsSearch({ state: 'open', view: 'all' })).toEqual({ view: 'all' });
    expect(approvalsSearch({ view: 'mine' })).toEqual({});
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
