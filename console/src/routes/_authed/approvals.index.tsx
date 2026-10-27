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
import { ENV_TITLE } from '../../app-detail/names';
import { Ago } from '../../components/Ago';
import { ApprovalBadge, approvalWord, Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { Loading } from '../../components/Loading';
import { PageHeader } from '../../components/PageHeader';
import { Person } from '../../components/Person';
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
        purpose={inbox ? 'Requests you may approve or reject; open one to see what it changes.' : 'Every request you may see, and how it was decided.'}
        about={
          <p>
            {inbox
              ? 'Org admins see every request, and a connection owner sees the sharing requests that go beyond their connection’s audience ceiling.'
              : 'Org admins see every request; others see their own.'}
          </p>
        }
      />
      <nav className="tabs" aria-label="Requests shown">
        {/* Both links lead to this path, so the first matches only an empty search: without that the
            router would mark it as the current page under ?view=all as well. */}
        <Link
          to="/approvals"
          search={{}}
          activeOptions={{ exact: true }}
          className={inbox ? 'current' : undefined}
          aria-current={inbox ? 'page' : undefined}
        >
          Waiting for you
        </Link>
        <Link
          to="/approvals"
          search={{ view: 'all' }}
          className={inbox ? undefined : 'current'}
          aria-current={inbox ? undefined : 'page'}
        >
          All requests
        </Link>
      </nav>
      {inbox ? null : (
        <div className="toolbar">
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
                {approvalWord(s)}
              </option>
            ))}
          </select>
        </div>
      )}
      {list.data ? (
        <section className="panel">
          <Table
            caption="Approval requests"
            onOpen={(a) => void navigate({ to: '/approvals/$approvalId', params: { approvalId: a.id } })}
            columns={COLUMNS}
            rows={rows}
            rowKey={(a) => a.id}
            empty={
              inbox
                ? 'Nothing is waiting for you.'
                : state
                  ? `No ${approvalWord(state).toLowerCase()} requests.`
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
        <Loading label="Loading approval requests…" />
      )}
    </>
  );
}

function summary(a: Approval): string {
  switch (a.kind) {
    case 'widen_audience':
      return 'Widen sharing';
    case 'agent_share': {
      const version = replacedVersion(a.payload);
      return `Sharing change by an agent${version === null ? '' : `, replacing version ${version}`}`;
    }
    case 'exceed_ceiling':
      return `Share beyond the ceiling of ${connectionName(a)}`;
    case 'connect_data_source':
      return `Connect ${a.subject_key}`;
    case 'enable_internet_hosts':
      return `Allow ${a.subject_key}`;
    default:
      return 'Request';
  }
}

function Request({ approval: a }: { readonly approval: Approval }) {
  const grants = requestedGrants(a.payload);
  const env = ENV_TITLE[a.environment as keyof typeof ENV_TITLE] ?? a.environment;
  return (
    <>
      <Link to="/approvals/$approvalId" params={{ approvalId: a.id }}>
        {summary(a)}
      </Link>
      <div className="muted">
        {a.app} {env}
        {grants && grants.length > 0 ? `, to ${grants.map((g) => grantText(g, a.subject_names)).join(', ')}` : ''}
      </div>
    </>
  );
}

const COLUMNS: readonly Column<Approval>[] = [
  { header: 'Request', cell: (a) => <Request approval={a} /> },
  {
    header: 'Requested by',
    cell: (a) => (
      <>
        <Person name={a.requested_by_name} email={a.requested_by_email} />
        {a.requested_via_agent ? (
          <>
            {' '}
            <Badge tone="info">agent</Badge>
          </>
        ) : null}
      </>
    ),
  },
  { header: 'Age', className: 'nowrap', cell: (a) => <Ago at={a.created_at} /> },
  { header: 'Status', cell: (a) => <ApprovalBadge state={a.state} /> },
];
