import { useRouteContext } from '@tanstack/react-router';
import { BILLING_TEXT, coldStarts, type Health, hours, size } from '../api/cell';
import type { AppOut, EnvironmentOut } from '../api/lifecycle';
import { Badge, type Tone } from '../components/Badge';

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

/**
 * How one environment is running: its state, where asleep is normal and the next request wakes
 * it; its use this month; and its database's connections and size. Each line says "not
 * available" rather than failing the page when the cell cannot answer.
 */
export function Running({ app, env }: { readonly app: AppOut; readonly env: EnvironmentOut }) {
  const { queries } = useRouteContext({ from: '/_authed/apps/$appId' });
  const init = { params: { path: { app_id: app.id, environment_id: env.id } } };
  const health = queries.useQuery(
    'get',
    '/v1/apps/{app_id}/environments/{environment_id}/health',
    init,
  );
  const usage = queries.useQuery('get', '/v1/apps/{app_id}/environments/{environment_id}/usage', init);
  const database = queries.useQuery(
    'get',
    '/v1/apps/{app_id}/environments/{environment_id}/database',
    init,
  );
  const state = health.data?.state ?? null;
  const db = database.data;

  return (
    <>
      <dt>Status</dt>
      <dd>
        {health.isPending ? (
          <span className="muted">Checking…</span>
        ) : health.isError ? (
          <span className="muted">Not available</span>
        ) : state ? (
          <>
            <Badge tone={HEALTH[state].tone}>{HEALTH[state].text}</Badge>
            {state === 'asleep' ? (
              <span className="muted"> Nobody is using it; the next visit wakes it.</span>
            ) : null}
          </>
        ) : (
          <span className="muted">{REASON[health.data.reason] ?? health.data.reason}</span>
        )}
      </dd>
      <dt>This month</dt>
      <dd>
        {usage.isPending ? (
          <span className="muted">Loading…</span>
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
          <span className="muted">Loading…</span>
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
