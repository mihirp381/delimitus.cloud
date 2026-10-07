import { Link, useRouteContext } from '@tanstack/react-router';
import { type FormEvent, useEffect, useId, useState } from 'react';
import { type AppOut, type EnvironmentOut, operationFinished, POLL_MS } from '../api/lifecycle';
import { ApiProblem } from '../api/problem';
import { buildFinished, type CapabilityChange, deployRelease, promote } from '../api/promote';
import { Badge, type Tone } from '../components/Badge';
import { Button } from '../components/Button';
import { ConfirmAction } from '../components/ConfirmAction';
import { Dialog } from '../components/Dialog';
import { ProblemNotice } from '../components/ProblemNotice';

const BUILD_TONE: Readonly<Record<string, Tone>> = {
  queued: 'info',
  running: 'info',
  succeeded: 'success',
  failed: 'danger',
};

const OP_TONE: Readonly<Record<string, Tone>> = {
  pending: 'info',
  running: 'info',
  healthy: 'success',
  failed: 'danger',
  superseded: 'neutral',
};

const SEVERITY_TONE: Readonly<Record<CapabilityChange['severity'], Tone>> = {
  high: 'danger',
  medium: 'warning',
  low: 'neutral',
};

interface Started {
  readonly buildId: string;
  /** The preview release the build was made from. */
  readonly fromNumber: number;
}

interface Props {
  readonly app: AppOut;
  readonly prod: EnvironmentOut;
  readonly preview: EnvironmentOut;
}

/**
 * Promote: production builds what preview runs now, then the person deploys the release that
 * build makes. Needs a builder on prod (the API refuses anyone else) and an active app.
 */
export function Promote({ app, prod, preview }: Props) {
  const [open, setOpen] = useState(false);
  const [started, setStarted] = useState<Started | null>(null);
  return (
    <>
      <Button
        variant="primary"
        disabled={app.status !== 'active'}
        aria-label="Promote preview to production"
        onClick={() => setOpen(true)}
      >
        Promote to production
      </Button>
      <Dialog open={open} title="Promote to production" onClose={() => setOpen(false)}>
        {open ? (
          <PromoteForm
            app={app}
            preview={preview}
            onClose={() => setOpen(false)}
            onStarted={(s) => {
              setStarted(s);
              setOpen(false);
            }}
          />
        ) : null}
      </Dialog>
      {started ? <BuildProgress key={started.buildId} app={app} prod={prod} started={started} /> : null}
    </>
  );
}

function ApprovalsLink({ error }: { readonly error: unknown }) {
  if (!(error instanceof ApiProblem) || error.code !== 'APPROVAL_REQUIRED') return null;
  return (
    <p>
      <Link to="/approvals">See the approvals</Link>
    </p>
  );
}

interface FormProps {
  readonly app: AppOut;
  readonly preview: EnvironmentOut;
  readonly onClose: () => void;
  readonly onStarted: (started: Started) => void;
}

function PromoteForm({ app, preview, onClose, onStarted }: FormProps) {
  const { api, queries } = useRouteContext({ from: '/_authed/apps/$appId' });
  const deployments = queries.useQuery('get', '/v1/apps/{app_id}/environments/{environment_id}/deployments', {
    params: { path: { app_id: app.id, environment_id: preview.id }, query: { limit: 50 } },
  });
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const confirmId = useId();
  const live = deployments.data?.items.find((d) => d.current) ?? null;
  const healthy = live !== null && live.state === 'healthy';
  const ready = healthy && typed === app.slug;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!live || !ready || busy) return;
    setBusy(true);
    setError(null);
    try {
      const accepted = await promote(api, app.id, live.release_id);
      onStarted({ buildId: accepted.build_id, fromNumber: live.release_number });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="stack" onSubmit={submit} aria-label="Promote to production">
      {deployments.isPending ? (
        <p className="muted">Loading what preview runs…</p>
      ) : deployments.isError ? (
        <ProblemNotice error={deployments.error} />
      ) : live === null ? (
        <p className="empty">Preview runs nothing yet, so there is nothing to promote.</p>
      ) : healthy ? (
        <p>
          Production builds <strong>release {live.release_number}</strong> of preview from the same
          source. Production keeps running what it runs now until you deploy the new release.
        </p>
      ) : (
        <p className="notice notice-warning" role="status">
          Release {live.release_number} in preview is {live.state}; promote needs preview to be
          healthy.
        </p>
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
      <ApprovalsLink error={error} />
      <div className="actions">
        <Button onClick={onClose}>Cancel</Button>
        <Button type="submit" variant="primary" disabled={!ready || busy}>
          {live ? `Promote release ${live.release_number}` : 'Promote'}
        </Button>
      </div>
    </form>
  );
}

function BuildProgress({
  app,
  prod,
  started,
}: {
  readonly app: AppOut;
  readonly prod: EnvironmentOut;
  readonly started: Started;
}) {
  const { api, queries } = useRouteContext({ from: '/_authed/apps/$appId' });
  const build = queries.useQuery(
    'get',
    '/v1/builds/{build_id}',
    { params: { path: { build_id: started.buildId } } },
    { refetchInterval: (q) => (buildFinished(q.state.data) ? false : POLL_MS) },
  );
  const [operationId, setOperationId] = useState<string | null>(null);
  if (build.isPending) return <p className="muted">Starting the build for production…</p>;
  if (build.isError) return <ProblemNotice error={build.error} />;
  const b = build.data;
  const changes = b.capability_diff.changes;
  return (
    <div className="stack" role="status" aria-label="Promote">
      <p>
        Build of preview release {started.fromNumber} for production{' '}
        <code className="muted">{b.build_id}</code>{' '}
        <Badge tone={BUILD_TONE[b.state] ?? 'neutral'}>{b.state}</Badge>
        {b.failure_code ? (
          <span>
            {' '}
            <code>{b.failure_code}</code>
          </span>
        ) : null}
      </p>
      {changes.length > 0 ? (
        <div>
          <p>
            The release asks for{' '}
            {b.capability_diff.total === 1 ? 'one thing' : `${b.capability_diff.total} things`}{' '}
            production does not have yet:
          </p>
          <ul>
            {changes.map((c) => (
              <li key={`${c.kind}:${c.subject}`}>
                <Badge tone={SEVERITY_TONE[c.severity]}>{c.severity}</Badge> {c.consequence}
                {c.approver ? <span className="muted"> (needs approval from {c.approver})</span> : null}
              </li>
            ))}
          </ul>
          {changes.some((c) => c.approver !== null) ? (
            <p>
              <Link to="/approvals">See the approvals</Link>
            </p>
          ) : null}
        </div>
      ) : null}
      {b.state === 'succeeded' && b.release_id !== null && b.release_number !== null ? (
        operationId ? (
          <DeployProgress app={app} operationId={operationId} releaseNumber={b.release_number} />
        ) : (
          <div className="toolbar">
            <span>Release {b.release_number} is built for production.</span>
            <ConfirmAction
              variant="secondary"
              label={`Deploy release ${b.release_number} to production`}
              title={`Deploy release ${b.release_number} to production`}
              confirmText={app.slug}
              confirmLabel="Deploy"
              onConfirm={async () => {
                const releaseId = b.release_id;
                if (releaseId === null) return;
                setOperationId(await deployRelease(api, app.id, prod.id, releaseId));
              }}
            >
              <p>
                Production of <strong>{app.slug}</strong> starts running release {b.release_number}.
                Everyone it is shared with gets the new version.
              </p>
            </ConfirmAction>
          </div>
        )
      ) : null}
    </div>
  );
}

function DeployProgress({
  app,
  operationId,
  releaseNumber,
}: {
  readonly app: AppOut;
  readonly operationId: string;
  readonly releaseNumber: number;
}) {
  const { queries, queryClient } = useRouteContext({ from: '/_authed/apps/$appId' });
  const op = queries.useQuery(
    'get',
    '/v1/operations/{operation_id}',
    { params: { path: { operation_id: operationId } } },
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
  if (op.isPending) return <p className="muted">Starting the deploy…</p>;
  if (op.isError) return <ProblemNotice error={op.error} />;
  return (
    <p>
      Deploy of release {releaseNumber} to production <code className="muted">{op.data.operation_id}</code>{' '}
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
