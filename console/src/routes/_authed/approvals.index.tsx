import { useInfiniteQuery } from '@tanstack/react-query';
import { createFileRoute, Link, useNavigate } from '@tanstack/react-router';
import { useId } from 'react';
import {
  APPROVAL_STATES,
  type Approval,
  approvalsSearch,
  connectionName,
  grantText,
  replacedVersion,
  requestedGrants,
} from '../../api/approvals';
import { must } from '../../api/client';
import { ApprovalBadge, Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { PageHeader } from '../../components/PageHeader';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';

export const Route = createFileRoute('/_authed/approvals/')({
  validateSearch: approvalsSearch,
  component: ApprovalsPage,
});

function ApprovalsPage() {
  const { api } = Route.useRouteContext();
  const { state, view } = approvalsSearch(Route.useSearch());
  const inbox = view !== 'all';
  const navigate = useNavigate({ from: Route.fullPath });
  const stateId = useId();
  const list = useInfiniteQuery({
    queryKey: ['approvals', inbox, inbox ? null : (state ?? null)],
    queryFn: async ({ pageParam, signal }) =>
      must(
        await api.GET('/v1/approvals', {
          params: {
            query: {
              ...(inbox ? { inbox: true } : state ? { state } : {}),
              ...(pageParam === null ? {} : { before: pageParam }),
            },
          },
          signal,
        }),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_before,
  });
  const rows = list.data?.pages.flatMap((p) => p.approvals) ?? [];
  const noun = inbox ? 'waiting for you' : '';

  return (
    <>
      <PageHeader
        title="Approvals"
        purpose={
          inbox
            ? 'Requests you may approve or reject: org admins see every one, and a connection owner sees the sharing requests that go beyond their connection’s audience ceiling. Open one to see what it changes.'
            : 'Every request you may see, and how it was decided. Org admins see every request; others see their own.'
        }
      >
        <div className="tabs">
          {/* The class marks the view on show. aria-current cannot: the router also sets it on the
              first link under ?view=all, since both links lead to this path. */}
          <Link
            to="/approvals"
            search={{}}
            className={inbox ? 'current' : undefined}
            aria-current={inbox ? 'page' : undefined}
          >
            Waiting for you
          </Link>{' '}
          <Link
            to="/approvals"
            search={{ view: 'all' }}
            className={inbox ? undefined : 'current'}
            aria-current={inbox ? undefined : 'page'}
          >
            All requests
          </Link>
          {inbox ? null : (
            <>
              <label htmlFor={stateId} className="visually-hidden">
                State
              </label>
              <select
                id={stateId}
                value={state ?? ''}
                onChange={(e) => {
                  const next = approvalsSearch({ view: 'all', state: e.target.value });
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
            </>
          )}
        </div>
      </PageHeader>
      {list.data ? (
        <section className="panel tone-violet">
          <Table
            caption="Approval requests"
            columns={COLUMNS}
            rows={rows}
            rowKey={(a) => a.id}
            empty={
              inbox
                ? 'Nothing is waiting for you.'
                : state
                  ? `No ${state} requests.`
                  : 'No approval requests yet.'
            }
            emptyHint={
              inbox
                ? 'A request appears here when it needs your decision. All requests shows the ones already decided.'
                : state
                  ? 'Choose another state, or Every state, to see the rest.'
                  : 'A request is opened when someone asks to widen sharing, connect a data source or allow an internet host.'
            }
          />
          <div className="more">
            <p className="muted count" aria-live="polite">
              {rows.length} {rows.length === 1 ? 'request' : 'requests'}
              {noun ? ` ${noun}` : ''}
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

function Where({ approval }: { readonly approval: Approval }) {
  return (
    <>
      <Link to="/apps/$appId" params={{ appId: approval.app_id }}>
        {approval.app}
      </Link>{' '}
      {approval.environment}
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
    case 'exceed_ceiling':
      return (
        <p>
          Share beyond the audience ceiling of the connection <code>{connectionName(approval)}</code>
        </p>
      );
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
  if (a.state === 'pending') return <span className="muted">Waiting for an approver</span>;
  if (a.state === 'cancelled') return <span className="muted">Withdrawn by the requester</span>;
  return (
    <>
      <p>
        {a.state === 'approved' ? 'Approved' : 'Denied'} by{' '}
        {a.decided_by_user_id ? <code>{a.decided_by_user_id}</code> : 'an unnamed approver'}
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
        {a.requested_by_name}
        {a.requested_via_agent ? (
          <>
            {' '}
            <Badge tone="info">agent</Badge>
          </>
        ) : null}
      </>
    ),
  },
  { header: 'State', cell: (a) => <ApprovalBadge state={a.state} /> },
  { header: 'Decision', cell: (a) => <Decision approval={a} /> },
  {
    header: 'Review',
    cell: (a) => (
      <Link to="/approvals/$approvalId" params={{ approvalId: a.id }}>
        Open
      </Link>
    ),
  },
];
