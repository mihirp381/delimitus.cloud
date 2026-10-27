import { Link, useRouteContext } from '@tanstack/react-router';
import { type AppDeleteRun, POLL_MS } from '../api/lifecycle';
import { Badge } from '../components/Badge';
import { Notice } from '../components/Notice';
import { ProblemNotice } from '../components/ProblemNotice';
import { type Column, Table } from '../components/Table';
import { clock, STATE_TONE, took } from './KillSwitchRun';

type Step = AppDeleteRun['steps'][number];

// Keyed by the generated union: a step the API adds or removes fails the typecheck.
const STEP_TEXT: Readonly<Record<Step['name'], string>> = {
  gateway_deny: 'Deny at the gateway',
  datagw_suspend: 'Suspend data connections',
  egress_remove: 'Remove internet access',
  scale_to_zero: 'Stop instances',
  remove_timers: 'Remove timers',
  drop_databases: 'Drop databases',
  delete_files: 'Delete files',
  delete_secrets: 'Delete secrets',
  delete_services: 'Delete services',
  delete_bundles: 'Delete stored bundles',
  remove_records: 'Remove sharing, requests and links',
};

const STEP_COLUMNS: readonly Column<Step>[] = [
  { header: 'Step', cell: (s) => STEP_TEXT[s.name] },
  { header: 'State', cell: (s) => <Badge tone={STATE_TONE[s.state] ?? 'neutral'}>{s.state}</Badge> },
  { header: 'Removed', cell: (s) => s.removed ?? '' },
  { header: 'Took', cell: (s) => took(s.elapsed_ms) },
  { header: 'Tries', cell: (s) => s.attempts },
  { header: 'Last error', cell: (s) => (s.error ? <code>{s.error}</code> : '') },
];

/** The page adds each step's own clock times between its state and what it removed. */
const PAGE_COLUMNS: readonly Column<Step>[] = [
  ...STEP_COLUMNS.slice(0, 2),
  { header: 'Started', cell: (s) => clock(s.started_at) },
  { header: 'Finished', cell: (s) => clock(s.finished_at) },
  ...STEP_COLUMNS.slice(2),
];

interface Props {
  readonly appId: string;
  readonly runId: string;
  /** The run's own page: both clock times and the total as a list, and no link to itself. */
  readonly page?: boolean;
}

/**
 * One removal of an app: its state and total, and each step's state, count and time. Read again
 * every second while it runs, and not after. It stays readable once the app itself is gone.
 */
export function DeleteProgress({ appId, runId, page = false }: Props) {
  const { queries } = useRouteContext({ from: '/_authed' });
  const run = queries.useQuery(
    'get',
    '/v1/apps/{app_id}/delete/{run_id}',
    { params: { path: { app_id: appId, run_id: runId } } },
    { refetchInterval: (q) => (q.state.data?.state === 'running' ? POLL_MS : false) },
  );
  if (run.isPending) return <p className="muted">Reading the delete run…</p>;
  if (run.isError) return <ProblemNotice error={run.error} />;
  const r = run.data;
  const state = <Badge tone={STATE_TONE[r.state] ?? 'neutral'}>{r.state}</Badge>;
  const failed = r.steps.find((s) => s.state === 'failed');
  return (
    <div className="stack" aria-label="Delete run" role="group">
      {page ? (
        <dl className="facts" aria-live="polite">
          <dt>State</dt>
          <dd>{state}</dd>
          <dt>Started</dt>
          <dd>
            <time dateTime={r.started_at}>{new Date(r.started_at).toLocaleString()}</time>
          </dd>
          <dt>Finished</dt>
          <dd>
            {r.finished_at === null ? (
              'Still running'
            ) : (
              <time dateTime={r.finished_at}>{new Date(r.finished_at).toLocaleString()}</time>
            )}
          </dd>
          <dt>Total</dt>
          <dd>{r.total_ms === null ? '…' : `${r.total_ms} ms (${(r.total_ms / 1000).toFixed(1)} s)`}</dd>
          <dt>Run id</dt>
          <dd>
            <code>{r.run_id}</code>
          </dd>
        </dl>
      ) : (
        <p aria-live="polite">
          Delete <code className="muted">{r.run_id}</code> {state}
          {r.total_ms === null ? null : <span className="muted"> in {r.total_ms} ms</span>}{' '}
          <Link to="/apps/$appId/delete/$runId" params={{ appId, runId }}>
            Open this run
          </Link>
        </p>
      )}
      {r.state === 'completed' ? (
        <p role="status">
          The app is deleted: its address no longer answers and its database places are free.{' '}
          <Link to="/">Back to Apps</Link>
        </p>
      ) : r.state === 'failed' ? (
        <Notice tone="warn">
          <p>
            The removal stopped{failed ? ` at "${STEP_TEXT[failed.name]}"` : ''}. What it removed
            before that stays removed, and the app stays stopped. Delete the app again from its
            Settings tab to continue.
          </p>
        </Notice>
      ) : null}
      <Table
        caption="Delete steps"
        columns={page ? PAGE_COLUMNS : STEP_COLUMNS}
        rows={r.steps}
        rowKey={(s) => s.name}
        empty="No step has started yet."
        emptyHint={r.state === 'running' ? 'The first step shows here within a second.' : undefined}
      />
    </div>
  );
}
