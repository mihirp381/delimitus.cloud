import { Link, useRouteContext } from '@tanstack/react-router';
import { type KillSwitchMode, type KillSwitchRun, POLL_MS } from '../api/lifecycle';
import { Badge, type Tone } from '../components/Badge';
import { ProblemNotice } from '../components/ProblemNotice';
import { type Column, Table } from '../components/Table';

type Step = KillSwitchRun['steps'][number];

export const MODE_TITLE: Readonly<Record<KillSwitchMode, string>> = {
  disable: 'Disable',
  quarantine: 'Quarantine',
};

const STEP_TEXT: Readonly<Record<Step['name'], string>> = {
  gateway_deny: 'Deny at the gateway',
  datagw_suspend: 'Suspend data connections',
  egress_remove: 'Remove internet access',
  scale_to_zero: 'Stop instances',
  pause_timers: 'Pause timers',
};

const STATE_TONE: Readonly<Record<string, Tone>> = {
  running: 'info',
  done: 'success',
  completed: 'success',
  unconfirmed: 'warning',
  failed: 'danger',
};

function took(ms: number | null): string {
  return ms === null ? '…' : `${ms} ms`;
}

function clock(at: string | null) {
  return at === null ? '…' : <time dateTime={at}>{new Date(at).toLocaleTimeString()}</time>;
}

const STEP_COLUMNS: readonly Column<Step>[] = [
  { header: 'Step', cell: (s) => STEP_TEXT[s.name] },
  { header: 'State', cell: (s) => <Badge tone={STATE_TONE[s.state] ?? 'neutral'}>{s.state}</Badge> },
  { header: 'Took', cell: (s) => took(s.elapsed_ms) },
  { header: 'Tries', cell: (s) => s.attempts },
  { header: 'Last error', cell: (s) => (s.error ? <code>{s.error}</code> : '') },
];

/** The page adds each step's own clock times between its state and how long it took. */
const PAGE_COLUMNS: readonly Column<Step>[] = [
  ...STEP_COLUMNS.slice(0, 2),
  { header: 'Started', cell: (s) => clock(s.started_at) },
  { header: 'Finished', cell: (s) => clock(s.finished_at) },
  ...STEP_COLUMNS.slice(2),
];

interface Props {
  readonly appId: string;
  readonly runId: string;
  /** The run's own page: the mode, both clock times and the total as a list, and no link to itself. */
  readonly page?: boolean;
}

/**
 * One pull of the kill switch: its mode, state and total, and each step's state and time. Read
 * again every second while it runs, and not after.
 */
export function KillSwitchProgress({ appId, runId, page = false }: Props) {
  const { queries } = useRouteContext({ from: '/_authed' });
  const run = queries.useQuery(
    'get',
    '/v1/apps/{app_id}/kill-switch/{run_id}',
    { params: { path: { app_id: appId, run_id: runId } } },
    { refetchInterval: (q) => (q.state.data?.state === 'running' ? POLL_MS : false) },
  );
  if (run.isPending) return <p className="muted">Reading the kill switch run…</p>;
  if (run.isError) return <ProblemNotice error={run.error} />;
  const r = run.data;
  const state = <Badge tone={STATE_TONE[r.state] ?? 'neutral'}>{r.state}</Badge>;
  return (
    <div className="stack" aria-label="Kill switch run" role="group">
      {page ? (
        <dl className="facts" aria-live="polite">
          <dt>Mode</dt>
          <dd>{MODE_TITLE[r.mode]}</dd>
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
          {MODE_TITLE[r.mode]} <code className="muted">{r.run_id}</code> {state}
          {r.total_ms === null ? null : <span className="muted"> in {r.total_ms} ms</span>}{' '}
          <Link to="/apps/$appId/kill-switch/$runId" params={{ appId, runId }}>
            Open this run
          </Link>
        </p>
      )}
      <Table
        caption="Kill switch steps"
        columns={page ? PAGE_COLUMNS : STEP_COLUMNS}
        rows={r.steps}
        rowKey={(s) => s.name}
        empty="No step has started yet."
        emptyHint={r.state === 'running' ? 'The first step shows here within a second.' : undefined}
      />
    </div>
  );
}
