import { useRouteContext } from '@tanstack/react-router';
import { type FormEvent, useEffect, useId, useState } from 'react';
import {
  type AppOut,
  type EnvironmentOut,
  operationFinished,
  POLL_MS,
  type Release,
  rollBack,
  runsIn,
} from '../api/lifecycle';
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

/**
 * Deploys an earlier release again, keeping today's config and sharing. Only releases that may
 * run in the environment can be picked: production runs only releases built for production.
 * A stopped app cannot be rolled back (APP_NOT_ACTIVE), so the button is off until it is enabled.
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
  const releases = queries.useQuery('get', '/v1/apps/{app_id}/releases', {
    params: { path: { app_id: app.id }, query: { limit: 50 } },
  });
  const deployments = queries.useQuery('get', '/v1/apps/{app_id}/environments/{environment_id}/deployments', {
    params: { path: { app_id: app.id, environment_id: env.id }, query: { limit: 50 } },
  });
  const [choice, setChoice] = useState<string | null>(null);
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const name = useId();
  const confirmId = useId();

  const live = deployments.data?.items.find((d) => d.current)?.release_id ?? null;
  const picked = releases.data?.items.find((r) => r.release_id === choice) ?? null;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!picked || typed !== app.slug || busy) return;
    setBusy(true);
    setError(null);
    try {
      const operationId = await rollBack(api, app.id, env.id, picked.release_id);
      onStarted({ operationId, label: `${ENV_TITLE[env.name]} to ${picked.label}` });
    } catch (e) {
      setError(e);
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
      ) : releases.isError ? (
        <ProblemNotice error={releases.error} />
      ) : deployments.isError ? (
        <ProblemNotice error={deployments.error} />
      ) : releases.data.items.length === 0 ? (
        <p className="empty">This app has no releases yet.</p>
      ) : (
        <fieldset className="choices">
          <legend>Release</legend>
          {releases.data.items.map((r) => {
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
      <div className="actions">
        <Button onClick={onClose}>Cancel</Button>
        <Button type="submit" variant="danger" disabled={!picked || typed !== app.slug || busy}>
          Roll back
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
