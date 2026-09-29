import { useInfiniteQuery } from '@tanstack/react-query';
import { createFileRoute, Link, useNavigate } from '@tanstack/react-router';
import { useId } from 'react';
import {
  APPROVAL_STATES,
  type Approval,
  type ApprovalState,
  approvalsSearch,
  grantText,
  replacedVersion,
  requestedGrants,
} from '../../api/approvals';
import { must } from '../../api/client';
import { Badge, type Tone } from '../../components/Badge';
import { Button } from '../../components/Button';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';

export const Route = createFileRoute('/_authed/approvals')({
  validateSearch: approvalsSearch,
  component: ApprovalsPage,
});

const STATE_TONE: Readonly<Record<ApprovalState, Tone>> = {
  pending: 'warning',
  approved: 'success',
  denied: 'danger',
  cancelled: 'neutral',
};

function ApprovalsPage() {
  const { api } = Route.useRouteContext();
  const { state } = approvalsSearch(Route.useSearch());
  const navigate = useNavigate({ from: Route.fullPath });
  const stateId = useId();
  const list = useInfiniteQuery({
    queryKey: ['approvals', state ?? null],
    queryFn: async ({ pageParam, signal }) =>
      must(
        await api.GET('/v1/approvals', {
          params: {
            query: { ...(state ? { state } : {}), ...(pageParam === null ? {} : { before: pageParam }) },
          },
          signal,
        }),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_before,
  });
  const rows = list.data?.pages.flatMap((p) => p.approvals) ?? [];

  return (
    <>
      <div className="page-head">
        <h1>Approvals</h1>
        <div className="search">
          <label htmlFor={stateId} className="visually-hidden">
            State
          </label>
          <select
            id={stateId}
            value={state ?? ''}
            onChange={(e) => {
              const next = approvalsSearch({ state: e.target.value });
              void navigate({ search: next });
            }}
          >
            <option value="">Every state</option>
            {APPROVAL_STATES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
      </div>
      <p className="muted">
        Changes waiting for, or given, an org admin's approval (decision 016). SSC staff record each
        decision from the admin's reply by email or chat; the console shows requests and outcomes
        and cannot decide. Org admins see every request; others see their own.
      </p>
      {list.data ? (
        <section className="panel">
          <Table
            caption="Approval requests"
            columns={COLUMNS}
            rows={rows}
            rowKey={(a) => a.id}
            empty={state ? `No ${state} requests.` : 'No approval requests yet.'}
          />
          <div className="more">
            <p className="muted count" aria-live="polite">
              {rows.length} {rows.length === 1 ? 'request' : 'requests'}
              {list.hasNextPage ? ', more are older' : ''}
            </p>
            {list.hasNextPage ? (
              <Button disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()}>
                {list.isFetchingNextPage ? 'Loading…' : 'Load older requests'}
              </Button>
            ) : null}
          </div>
          {list.isError ? <ProblemNotice error={list.error} /> : null}
        </section>
      ) : list.isError ? (
        <ProblemNotice error={list.error} />
      ) : (
        <p className="muted">Loading approval requests…</p>
      )}
    </>
  );
}

/** The app's slug and the environment's name, or their ids when the app cannot be read. */
function Where({ approval }: { readonly approval: Approval }) {
  const { queries } = Route.useRouteContext();
  const app = queries.useQuery('get', '/v1/apps/{app_id}', {
    params: { path: { app_id: approval.app_id } },
  });
  const env = app.data?.environments.find((e) => e.id === approval.environment_id);
  return (
    <>
      <Link to="/apps/$appId" params={{ appId: approval.app_id }}>
        {app.data ? app.data.slug : <code>{approval.app_id}</code>}
      </Link>{' '}
      {env ? env.name : <code className="muted">{approval.environment_id}</code>}
    </>
  );
}

function Grants({ approval }: { readonly approval: Approval }) {
  const grants = requestedGrants(approval.payload);
  if (grants === null) return <code>{approval.subject_key}</code>;
  if (grants.length === 0) return <p>Nobody has access.</p>;
  return (
    <ul className="plain-list">
      {grants.map((g) => (
        <li key={`${g.role}:${g.subject_kind}:${g.subject_id ?? ''}`}>{grantText(g)}</li>
      ))}
    </ul>
  );
}

function Change({ approval }: { readonly approval: Approval }) {
  switch (approval.kind) {
    case 'widen_audience':
      return (
        <>
          <p>Widen who has access, to:</p>
          <Grants approval={approval} />
        </>
      );
    case 'agent_share': {
      const version = replacedVersion(approval.payload);
      return (
        <>
          <p>
            Sharing change made by an agent
            {version === null ? '' : `, replacing version ${version}`}, to:
          </p>
          <Grants approval={approval} />
        </>
      );
    }
    case 'connect_data_source':
      return (
        <p>
          Connect the data source <code>{approval.subject_key}</code>
        </p>
      );
    case 'enable_internet_hosts':
      return (
        <p>
          Allow the internet host <code>{approval.subject_key}</code>
        </p>
      );
    default:
      return <code>{approval.subject_key}</code>;
  }
}

function Decision({ approval: a }: { readonly approval: Approval }) {
  if (a.state === 'pending') return <span className="muted">Waiting for an org admin</span>;
  if (a.state === 'cancelled') return <span className="muted">Withdrawn by the requester</span>;
  return (
    <>
      <p>
        {a.state === 'approved' ? 'Approved' : 'Denied'} by{' '}
        {a.decided_by_user_id ? <code>{a.decided_by_user_id}</code> : 'an unnamed admin'}
        {a.decision_channel ? ` by ${a.decision_channel}` : ''}
        {a.decided_at ? (
          <>
            {' '}
            on <time dateTime={a.decided_at}>{new Date(a.decided_at).toLocaleString()}</time>
          </>
        ) : null}
      </p>
      {a.decision_reason ? <p>“{a.decision_reason}”</p> : null}
      {a.recorded_by_operator ? (
        <p className="muted">
          Recorded by SSC staff <code>{a.recorded_by_operator}</code>
        </p>
      ) : null}
    </>
  );
}

const COLUMNS: readonly Column<Approval>[] = [
  {
    header: 'Requested',
    cell: (a) => <time dateTime={a.created_at}>{new Date(a.created_at).toLocaleString()}</time>,
  },
  { header: 'App', cell: (a) => <Where approval={a} /> },
  { header: 'Change', cell: (a) => <Change approval={a} /> },
  {
    header: 'Requested by',
    cell: (a) => (
      <>
        <code>{a.requested_by_user_id}</code>
        {a.requested_via_agent ? (
          <>
            {' '}
            <Badge tone="info">agent</Badge>
          </>
        ) : null}
      </>
    ),
  },
  { header: 'State', cell: (a) => <Badge tone={STATE_TONE[a.state]}>{a.state}</Badge> },
  { header: 'Decision', cell: (a) => <Decision approval={a} /> },
];
