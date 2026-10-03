import { useInfiniteQuery } from '@tanstack/react-query';
import { useRouteContext } from '@tanstack/react-router';
import { type FormEvent, useEffect, useId, useState } from 'react';
import { must } from '../api/client';
import {
  type AppOut,
  type EnvironmentOut,
  type LedgerAhead,
  migrationsAhead,
  operationFinished,
  POLL_MS,
  type Release,
  rollBack,
  runsIn,
} from '../api/lifecycle';
import { ApiProblem } from '../api/problem';
import { Badge, type Tone } from '../components/Badge';
import { Button } from '../components/Button';
import { Dialog } from '../components/Dialog';
import { ProblemNotice } from '../components/ProblemNotice';
import { ENV_TITLE } from './names';

const OP_TONE: Readonly<Record<string, Tone>> = {
  pending: 'info',
  running: 'info',
  healthy: 'success',
  failed: 'danger',
  superseded: 'neutral',
};

interface Started {
  readonly operationId: string;
  readonly label: string;
}

interface Ahead {
  readonly releaseId: string;
  readonly ledgers: readonly LedgerAhead[];
}

/**
 * Deploys an earlier release again, keeping today's config and sharing. Only releases that may
 * run in the environment can be picked: production runs only releases built for production.
 * A stopped app cannot be rolled back (APP_NOT_ACTIVE), so the button is off until it is enabled.
 * Releases are app-wide, newest first, so older pages load on request. A rollback does not undo
 * database migrations: on SCHEMA_AHEAD the dialog lists the ones the release lacks and goes
 * ahead only once the person ticks that the release works with them.
 */
export function Rollback({ app, env }: { readonly app: AppOut; readonly env: EnvironmentOut }) {
  const [open, setOpen] = useState(false);
  const [started, setStarted] = useState<Started | null>(null);
  const title = `Roll back ${ENV_TITLE[env.name].toLowerCase()}`;
  return (
    <>
      <Button
        onClick={() => setOpen(true)}
        disabled={app.status !== 'active'}
        aria-label={`Roll back ${ENV_TITLE[env.name]}`}
      >
        Roll back
      </Button>
      {started ? <RollbackProgress app={app} started={started} /> : null}
      <Dialog open={open} title={title} onClose={() => setOpen(false)}>
        {open ? (
          <RollbackForm
            app={app}
            env={env}
            title={title}
            onClose={() => setOpen(false)}
            onStarted={(s) => {
              setStarted(s);
              setOpen(false);
            }}
          />
        ) : null}
      </Dialog>
    </>
  );
}

interface FormProps {
  readonly app: AppOut;
  readonly env: EnvironmentOut;
  readonly title: string;
  readonly onClose: () => void;
  readonly onStarted: (started: Started) => void;
}

function RollbackForm({ app, env, title, onClose, onStarted }: FormProps) {
  const { api, queries } = useRouteContext({ from: '/_authed/apps/$appId' });
  const releases = useInfiniteQuery({
    queryKey: ['releases', app.id],
    queryFn: async ({ pageParam, signal }) =>
      must(
        await api.GET('/v1/apps/{app_id}/releases', {
          params: {
            path: { app_id: app.id },
            query: { limit: 50, ...(pageParam === null ? {} : { before: pageParam }) },
          },
          signal,
        }),
      ),
    initialPageParam: null as number | null,
    getNextPageParam: (page) => page.next_before,
  });
  const deployments = queries.useQuery('get', '/v1/apps/{app_id}/environments/{environment_id}/deployments', {
    params: { path: { app_id: app.id, environment_id: env.id }, query: { limit: 50 } },
  });
  const [choice, setChoice] = useState<string | null>(null);
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [ahead, setAhead] = useState<Ahead | null>(null);
  const [accepted, setAccepted] = useState(false);
  const name = useId();
  const confirmId = useId();
  const acceptId = useId();

  const live = deployments.data?.items.find((d) => d.current)?.release_id ?? null;
  const items = releases.data?.pages.flatMap((p) => p.items) ?? [];
  const picked = items.find((r) => r.release_id === choice) ?? null;
  const warned = ahead !== null && ahead.releaseId === choice ? ahead : null;
  const ready = picked !== null && typed === app.slug && (warned === null || accepted);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!picked || !ready || busy) return;
    setBusy(true);
    setError(null);
    try {
      const operationId = await rollBack(api, app.id, env.id, picked.release_id, warned !== null);
      onStarted({ operationId, label: `${ENV_TITLE[env.name]} to ${picked.label}` });
    } catch (e) {
      if (e instanceof ApiProblem && e.code === 'SCHEMA_AHEAD') {
        try {
          setAccepted(false);
          setAhead({
            releaseId: picked.release_id,
            ledgers: await migrationsAhead(api, app.id, env.id, picked.release_id),
          });
        } catch (inner) {
          setError(inner);
        }
      } else {
        setError(e);
      }
    } finally {
      setBusy(false);
    }
  }

  function why(r: Release): string | null {
    if (r.release_id === live) return 'live now';
    if (!runsIn(r, env)) {
      return env.name === 'prod' ? 'built for preview, not production' : 'built for production';
    }
    return null;
  }

  return (
    <form className="stack" onSubmit={submit} aria-label={title}>
      {env.name === 'prod' ? (
        <p className="muted">Production runs only releases built for production.</p>
      ) : null}
      {releases.isPending || deployments.isPending ? (
        <p className="muted">Loading releases…</p>
      ) : releases.isError && items.length === 0 ? (
        <ProblemNotice error={releases.error} />
      ) : deployments.isError ? (
        <ProblemNotice error={deployments.error} />
      ) : items.length === 0 ? (
        <p className="empty">This app has no releases yet.</p>
      ) : (
        <fieldset className="choices">
          <legend>Release</legend>
          {items.map((r) => {
            const blocked = why(r);
            return (
              <label key={r.release_id} className="check">
                <input
                  type="radio"
                  name={name}
                  checked={choice === r.release_id}
                  disabled={blocked !== null}
                  onChange={() => setChoice(r.release_id)}
                />
                <span>
                  {r.label}{' '}
                  <time className="muted" dateTime={r.created_at}>
                    {new Date(r.created_at).toLocaleString()}
                  </time>
                  {r.source_commit ? <code className="muted"> {r.source_commit.slice(0, 12)}</code> : null}
                  {blocked ? <span className="muted"> · {blocked}</span> : null}
                </span>
              </label>
            );
          })}
        </fieldset>
      )}
      {releases.hasNextPage && !deployments.isPending && !deployments.isError ? (
        <div className="more">
          {items.every((r) => why(r) !== null) ? (
            <p className="muted" aria-live="polite">
              None of these releases can be picked; an older one may.
            </p>
          ) : null}
          <Button disabled={releases.isFetchingNextPage} onClick={() => void releases.fetchNextPage()}>
            {releases.isFetchingNextPage ? 'Loading…' : 'Load older releases'}
          </Button>
        </div>
      ) : null}
      {releases.isError && items.length > 0 ? <ProblemNotice error={releases.error} /> : null}
      <label className="field" htmlFor={confirmId}>
        <span>
          Type <code>{app.slug}</code> to confirm
        </span>
        <input
          id={confirmId}
          value={typed}
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setTyped(e.target.value)}
        />
      </label>
      {error ? <ProblemNotice error={error} /> : null}
      {warned && picked ? (
        <div className="notice notice-warning" role="alert">
          <p>
            The database may have run migrations {picked.label} does not have. A rollback does not
            undo them, so {picked.label} may not work against the database as it is now.
          </p>
          <ul>
            {warned.ledgers.flatMap((l) =>
              l.names.map((n) => (
                <li key={`${l.ledger}:${n}`}>
                  <code>{n}</code> <span className="muted">({l.ledger})</span>
                </li>
              )),
            )}
          </ul>
          <label className="check" htmlFor={acceptId}>
            <input
              id={acceptId}
              type="checkbox"
              checked={accepted}
              onChange={(e) => setAccepted(e.target.checked)}
            />
            <span>{picked.label} works with these migrations</span>
          </label>
        </div>
      ) : null}
      <div className="actions">
        <Button onClick={onClose}>Cancel</Button>
        <Button type="submit" variant="danger" disabled={!ready || busy}>
          {warned ? 'Roll back anyway' : 'Roll back'}
        </Button>
      </div>
    </form>
  );
}

function RollbackProgress({ app, started }: { readonly app: AppOut; readonly started: Started }) {
  const { queries, queryClient } = useRouteContext({ from: '/_authed/apps/$appId' });
  const op = queries.useQuery(
    'get',
    '/v1/operations/{operation_id}',
    { params: { path: { operation_id: started.operationId } } },
    { refetchInterval: (q) => (operationFinished(q.state.data) ? false : POLL_MS) },
  );
  const finished = operationFinished(op.data);
  useEffect(() => {
    if (!finished) return;
    void queryClient.invalidateQueries({
      queryKey: queries.queryOptions('get', '/v1/apps/{app_id}', {
        params: { path: { app_id: app.id } },
      }).queryKey,
    });
  }, [finished, queries, queryClient, app.id]);
  if (op.isPending) return <p className="muted">Starting the rollback…</p>;
  if (op.isError) return <ProblemNotice error={op.error} />;
  return (
    <p role="status">
      Rollback of {started.label} <code className="muted">{op.data.operation_id}</code>{' '}
      <Badge tone={OP_TONE[op.data.state] ?? 'neutral'}>{op.data.state}</Badge>
      {op.data.failure_code ? (
        <span>
          {' '}
          <code>{op.data.failure_code}</code>
        </span>
      ) : null}
    </p>
  );
}
