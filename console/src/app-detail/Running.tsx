import { useRouteContext } from '@tanstack/react-router';
import { BILLING_TEXT, coldStarts, type Health, hours, size } from '../api/cell';
import type { AppOut, EnvironmentOut } from '../api/lifecycle';
import { Badge, type Tone } from '../components/Badge';
import { Loading } from '../components/Loading';

const HEALTH: Readonly<Record<NonNullable<Health['state']>, { tone: Tone; text: string }>> = {
  running: { tone: 'success', text: 'running' },
  asleep: { tone: 'neutral', text: 'asleep' },
  failing: { tone: 'danger', text: 'failing' },
};

const REASON: Readonly<Record<string, string>> = {
  not_deployed: 'Not deployed yet',
  stopped: 'Stopped',
  logs_unavailable: 'The cell cannot tell right now',
};

interface Props {
  readonly app: AppOut;
  readonly env: EnvironmentOut;
}

export function useHealth({ app, env }: Props) {
  const { queries } = useRouteContext({ from: '/_authed/apps/$appId' });
  return queries.useQuery('get', '/v1/apps/{app_id}/environments/{environment_id}/health', {
    params: { path: { app_id: app.id, environment_id: env.id } },
  });
}

export function HealthBadge({ health }: { readonly health: ReturnType<typeof useHealth> }) {
  if (health.isPending) return <span className="muted">Checking…</span>;
  if (health.isError) return <span className="muted">Not available</span>;
  const state = health.data.state;
  if (state) return <Badge tone={HEALTH[state].tone}>{HEALTH[state].text}</Badge>;
  return <Badge tone="neutral">{REASON[health.data.reason] ?? health.data.reason}</Badge>;
}

/** `dt`/`dd` pairs for a `dl`: asleep explained, use this month, the database. */
export function RunningFacts({ app, env, asleep }: Props & { readonly asleep: boolean }) {
  const { queries } = useRouteContext({ from: '/_authed/apps/$appId' });
  const init = { params: { path: { app_id: app.id, environment_id: env.id } } };
  const usage = queries.useQuery('get', '/v1/apps/{app_id}/environments/{environment_id}/usage', init);
  const database = queries.useQuery('get', '/v1/apps/{app_id}/environments/{environment_id}/database', init);
  const db = database.data;

  return (
    <>
      {asleep ? (
        <>
          <dt>Asleep</dt>
          <dd className="muted">Nobody is using it; the next visit wakes it.</dd>
        </>
      ) : null}
      <dt>This month</dt>
      <dd>
        {usage.isPending ? (
          <Loading label="Loading use this month…" inline />
        ) : usage.isError ? (
          <span className="muted">Not available</span>
        ) : (
          <>
            {usage.data.billing ? `${BILLING_TEXT[usage.data.billing]}, ` : ''}
            {hours(usage.data.session_hours)} of sessions, {hours(usage.data.instance_hours)}{' '}
            running. Cold starts: {coldStarts(usage.data)}.
          </>
        )}
      </dd>
      <dt>Database</dt>
      <dd>
        {database.isPending ? (
          <Loading label="Loading the database…" inline />
        ) : database.isError || !db ? (
          <span className="muted">Not available</span>
        ) : !db.present ? (
          'None'
        ) : (
          <>
            {db.connections === null ? 'Connections not known' : `${db.connections} connections`}
            {db.connection_limit === null ? '' : ` of ${db.connection_limit}`}
            {db.size_bytes === null ? '' : `, ${size(db.size_bytes)}`}
          </>
        )}
      </dd>
    </>
  );
}
