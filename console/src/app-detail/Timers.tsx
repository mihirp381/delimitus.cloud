import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useRouteContext } from '@tanstack/react-router';
import { useEffect, useRef, useState } from 'react';
import type { AppOut, EnvironmentOut } from '../api/lifecycle';
import { POLL_MS } from '../api/lifecycle';
import {
  listRuns,
  PAUSE_TEXT,
  pauseSchedule,
  readRun,
  resumeSchedule,
  RUN_ERROR_TEXT,
  runFinished,
  runNow,
  type Schedule,
  type ScheduleTarget,
  type TimerRun,
} from '../api/timers';
import { Badge, type Tone } from '../components/Badge';
import { Button } from '../components/Button';
import { ConfirmAction } from '../components/ConfirmAction';
import { Dialog } from '../components/Dialog';
import { ProblemNotice } from '../components/ProblemNotice';
import { type Column, Table } from '../components/Table';
import { ENV_TITLE } from './names';

interface Props {
  readonly app: AppOut;
  readonly env: EnvironmentOut;
}

const PATH = '/v1/apps/{app_id}/environments/{environment_id}/schedules' as const;

export const RUN_TONE: Readonly<Record<TimerRun['state'], Tone>> = {
  queued: 'info',
  running: 'info',
  succeeded: 'success',
  failed: 'danger',
  timed_out: 'danger',
  skipped: 'neutral',
};

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : '';
}

function took(run: TimerRun): string {
  return run.duration_ms === null ? '' : `${(run.duration_ms / 1000).toFixed(1)} s`;
}

interface Started {
  readonly target: ScheduleTarget;
  readonly name: string;
  readonly runId: string;
}

/**
 * The timers an environment's app declared, read only once the section is opened: when each
 * runs next, pause and resume, its runs, and a run now. Each needs a builder of the app; the
 * API refuses anyone else.
 */
export function Timers({ app, env }: Props) {
  const [open, setOpen] = useState(false);
  return (
    <details onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>Timers</summary>
      {open ? <TimersBody app={app} env={env} /> : null}
    </details>
  );
}

function TimersBody({ app, env }: Props) {
  const { api, queries, queryClient } = useRouteContext({ from: '/_authed/apps/$appId' });
  const init = { params: { path: { app_id: app.id, environment_id: env.id } } };
  const schedules = queries.useQuery('get', PATH, init);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [started, setStarted] = useState<Started | null>(null);
  const [history, setHistory] = useState<Schedule | null>(null);
  const where = ENV_TITLE[env.name];

  function targetOf(s: Schedule): ScheduleTarget {
    return { appId: app.id, environmentId: env.id, scheduleId: s.schedule_id };
  }

  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: queries.queryOptions('get', PATH, init).queryKey });
  }

  async function toggle(s: Schedule) {
    if (busy) return;
    setBusy(s.schedule_id);
    setError(null);
    setNotice(null);
    try {
      const pausing = s.state === 'active';
      const after = pausing ? await pauseSchedule(api, targetOf(s)) : await resumeSchedule(api, targetOf(s));
      await refresh();
      setNotice(
        pausing
          ? `Paused ${after.name}; it runs again only once someone resumes it.`
          : `Resumed ${after.name}; it now runs on your authority.`,
      );
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  }

  const columns: readonly Column<Schedule>[] = [
    {
      header: 'Timer',
      cell: (s) => (
        <>
          {s.name}{' '}
          <code className="muted">
            {s.method} {s.path}
          </code>
        </>
      ),
    },
    {
      header: 'Schedule',
      cell: (s) => (
        <>
          <code>{s.cron}</code> <span className="muted">{s.timezone}</span>
        </>
      ),
    },
    {
      header: 'Next run',
      cell: (s) =>
        s.next_run_at ? <time dateTime={s.next_run_at}>{when(s.next_run_at)}</time> : 'Not scheduled',
    },
    {
      header: 'State',
      cell: (s) =>
        s.state === 'active' ? (
          <Badge tone="success">active</Badge>
        ) : (
          <>
            <Badge tone="warning">{s.state}</Badge>
            {s.pause_reason ? <span className="muted"> {PAUSE_TEXT[s.pause_reason]}</span> : null}
          </>
        ),
    },
    {
      header: 'Last run',
      cell: (s) =>
        s.last_run ? (
          <>
            <Badge tone={RUN_TONE[s.last_run.state]}>{s.last_run.state}</Badge>{' '}
            <time className="muted" dateTime={s.last_run.scheduled_for}>
              {when(s.last_run.scheduled_for)}
            </time>
          </>
        ) : (
          'Never'
        ),
    },
    {
      header: 'Action',
      className: 'actions-cell',
      cell: (s) => (
        <div className="toolbar">
          {s.state === 'deleted' ? null : (
            <Button
              disabled={busy !== null}
              aria-label={`${s.state === 'active' ? 'Pause' : 'Resume'} ${s.name} in ${where}`}
              onClick={() => void toggle(s)}
            >
              {s.state === 'active' ? 'Pause' : 'Resume'}
            </Button>
          )}
          <ConfirmAction
            variant="secondary"
            label="Run now"
            accessibleLabel={`Run ${s.name} now in ${where}`}
            title={`Run ${s.name} now`}
            confirmLabel="Run now"
            disabled={app.status !== 'active' || s.state === 'deleted'}
            onConfirm={async () => {
              const target = targetOf(s);
              setNotice(null);
              setStarted({ target, name: s.name, runId: await runNow(api, target) });
            }}
          >
            <p>
              Calls <code>
                {s.method} {s.path}
              </code>{' '}
              on {where.toLowerCase()} of <strong>{app.slug}</strong> once, now, whether or not the
              timer is paused.
            </p>
          </ConfirmAction>
          <Button aria-label={`Runs of ${s.name} in ${where}`} onClick={() => setHistory(s)}>
            Runs
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="stack">
      {notice ? (
        <p className="notice notice-success" role="status">
          {notice}
        </p>
      ) : null}
      {error ? <ProblemNotice error={error} /> : null}
      {started ? <RunProgress key={started.runId} started={started} onFinished={refresh} /> : null}
      {schedules.isPending ? (
        <p className="muted">Loading timers…</p>
      ) : schedules.isError ? (
        <ProblemNotice error={schedules.error} />
      ) : (
        <Table
          caption={`Timers of ${where}`}
          columns={columns}
          rows={schedules.data.items}
          rowKey={(s) => s.schedule_id}
          empty="This environment's app declares no timers."
        />
      )}
      {env.name === 'preview' ? (
        <p className="muted">Timers never run on time in preview; run one now to try it.</p>
      ) : null}
      <Dialog
        open={history !== null}
        title={history ? `Runs of ${history.name}` : 'Runs'}
        onClose={() => setHistory(null)}
      >
        {history ? (
          <RunHistory target={targetOf(history)} onClose={() => setHistory(null)} />
        ) : null}
      </Dialog>
    </div>
  );
}

function RunProgress({
  started,
  onFinished,
}: {
  readonly started: Started;
  readonly onFinished: () => Promise<void>;
}) {
  const { api } = useRouteContext({ from: '/_authed/apps/$appId' });
  const run = useQuery({
    queryKey: ['timer-run', started.target.environmentId, started.target.scheduleId, started.runId],
    queryFn: ({ signal }) => readRun(api, started.target, started.runId, signal),
    refetchInterval: (q) => (runFinished(q.state.data) ? false : POLL_MS),
  });
  const finished = runFinished(run.data);
  const told = useRef(false);
  useEffect(() => {
    if (!finished || told.current) return;
    told.current = true;
    void onFinished();
  }, [finished, onFinished]);
  if (run.isPending) return <p className="muted">Starting the run…</p>;
  if (run.isError) return <ProblemNotice error={run.error} />;
  return (
    <p role="status">
      Run of {started.name} <code className="muted">{run.data.run_id}</code>{' '}
      <Badge tone={RUN_TONE[run.data.state]}>{run.data.state}</Badge>
      {run.data.http_status !== null ? <span className="muted"> HTTP {run.data.http_status}</span> : null}
      {run.data.error ? <span> {RUN_ERROR_TEXT[run.data.error]}</span> : null}
    </p>
  );
}

function RunHistory({ target, onClose }: { readonly target: ScheduleTarget; readonly onClose: () => void }) {
  const { api } = useRouteContext({ from: '/_authed/apps/$appId' });
  const [picked, setPicked] = useState<string | null>(null);
  const runs = useInfiniteQuery({
    queryKey: ['timer-runs', target.environmentId, target.scheduleId],
    queryFn: ({ pageParam, signal }) => listRuns(api, target, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_before,
  });
  const items = runs.data?.pages.flatMap((p) => p.items) ?? [];
  const columns: readonly Column<TimerRun>[] = [
    {
      header: 'Run',
      cell: (r) => (
        <Button aria-label={`Details of run ${r.run_id}`} onClick={() => setPicked(r.run_id)}>
          <time dateTime={r.scheduled_for}>{when(r.scheduled_for)}</time>
        </Button>
      ),
    },
    { header: 'State', cell: (r) => <Badge tone={RUN_TONE[r.state]}>{r.state}</Badge> },
    { header: 'Trigger', cell: (r) => (r.trigger === 'manual' ? 'run now' : 'on time') },
    { header: 'Took', cell: took },
    { header: 'Why', cell: (r) => (r.error ? RUN_ERROR_TEXT[r.error] : null) },
  ];
  return (
    <div className="stack">
      {runs.isPending ? (
        <p className="muted">Loading runs…</p>
      ) : runs.isError && items.length === 0 ? (
        <ProblemNotice error={runs.error} />
      ) : (
        <Table caption="Runs, newest first" columns={columns} rows={items} rowKey={(r) => r.run_id} empty="No runs yet." />
      )}
      {runs.hasNextPage ? (
        <div className="more">
          <Button disabled={runs.isFetchingNextPage} onClick={() => void runs.fetchNextPage()}>
            {runs.isFetchingNextPage ? 'Loading…' : 'Load older runs'}
          </Button>
        </div>
      ) : null}
      {runs.isError && items.length > 0 ? <ProblemNotice error={runs.error} /> : null}
      {picked ? <RunDetail target={target} runId={picked} /> : null}
      <div className="actions">
        <Button onClick={onClose}>Close</Button>
      </div>
    </div>
  );
}

function RunDetail({ target, runId }: { readonly target: ScheduleTarget; readonly runId: string }) {
  const { api } = useRouteContext({ from: '/_authed/apps/$appId' });
  const run = useQuery({
    queryKey: ['timer-run', target.environmentId, target.scheduleId, runId],
    queryFn: ({ signal }) => readRun(api, target, runId, signal),
  });
  if (run.isPending) return <p className="muted">Loading the run…</p>;
  if (run.isError) return <ProblemNotice error={run.error} />;
  const r = run.data;
  return (
    <section aria-label={`Run ${r.run_id}`}>
      <h3>
        Run <code>{r.run_id}</code>
      </h3>
      <dl className="facts">
        <dt>State</dt>
        <dd>
          <Badge tone={RUN_TONE[r.state]}>{r.state}</Badge>
        </dd>
        <dt>Trigger</dt>
        <dd>
          {r.trigger === 'manual' ? 'run now' : 'on time'}
          {r.requested_by_user_id ? (
            <>
              {' '}
              by <code>{r.requested_by_user_id}</code>
            </>
          ) : null}
        </dd>
        <dt>Due</dt>
        <dd>{when(r.scheduled_for)}</dd>
        <dt>Started</dt>
        <dd>{when(r.started_at) || 'Not started'}</dd>
        <dt>Finished</dt>
        <dd>{when(r.finished_at) || 'Not finished'}</dd>
        <dt>Took</dt>
        <dd>{took(r) || 'Not finished'}</dd>
        <dt>HTTP status</dt>
        <dd>{r.http_status ?? 'None'}</dd>
        <dt>Why it did not succeed</dt>
        <dd>{r.error ? `${RUN_ERROR_TEXT[r.error]} (${r.error})` : 'Nothing went wrong'}</dd>
      </dl>
    </section>
  );
}
