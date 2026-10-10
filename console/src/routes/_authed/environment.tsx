import { createFileRoute, Link } from '@tanstack/react-router';
import { type FormEvent, useState } from 'react';
import {
  BILLING_TEXT,
  CAUSE_TEXT,
  type CellDatabase,
  type CellEnvironment,
  type CellResource,
  coldStarts,
  hours,
  monthly,
  RESOURCE_TITLE,
  type Usage,
  when,
} from '../../api/cell';
import {
  GATEWAY_POLL_MS,
  setWarm,
  type WarmGateway,
  type WarmOut,
  warmCost,
} from '../../api/warm';
import { ENV_TITLE } from '../../app-detail/names';
import { Badge, type Tone } from '../../components/Badge';
import { Button } from '../../components/Button';
import { PageHeader } from '../../components/PageHeader';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';

export const Route = createFileRoute('/_authed/environment')({
  component: EnvironmentPage,
});

const STATE_TONE: Readonly<Record<CellResource['state'], Tone>> = {
  off: 'neutral',
  requested: 'info',
  creating: 'info',
  ready: 'success',
  failed: 'danger',
};

function EnvironmentPage() {
  const { queries, isAdmin } = Route.useRouteContext();
  const me = queries.useQuery('get', '/v1/whoami');
  if (isAdmin(me.data)) {
    return <YourEnvironment />;
  }
  return (
    <>
      <PageHeader title="Your environment" />
      {me.isPending ? (
        <p className="muted">Loading…</p>
      ) : me.isError ? (
        <ProblemNotice error={me.error} />
      ) : (
        <p className="muted">Only org admins can see the environment.</p>
      )}
    </>
  );
}

function YourEnvironment() {
  const { queries } = Route.useRouteContext();
  const cell = queries.useQuery('get', '/v1/cell');
  const usage = queries.useQuery('get', '/v1/usage');
  return (
    <>
      <PageHeader
        title="Your environment"
        purpose={
          <>
            What your company&apos;s cell runs and what each part adds a month. These figures are
            not a bill: you pay a flat fee, and they show what a choice sets off.
          </>
        }
      />
      <OutboundIp />
      {cell.isPending ? (
        <p className="muted">Loading the cell…</p>
      ) : cell.isError ? (
        <ProblemNotice error={cell.error} />
      ) : (
        <>
          <Resources resources={cell.data.resources} />
          <WarmPanel />
          <DatabasePanel database={cell.data.database} environments={cell.data.environments} />
          <section className="panel tone-orange" aria-labelledby="usage-heading">
            <h2 id="usage-heading">Usage this month</h2>
            {usage.isPending ? (
              <p className="muted">Loading usage…</p>
            ) : usage.isError ? (
              <ProblemNotice error={usage.error} />
            ) : (
              <UsageTable usage={usage.data.environments} environments={cell.data.environments} />
            )}
          </section>
        </>
      )}
    </>
  );
}

function OutboundIp() {
  const { queries } = Route.useRouteContext();
  const egress = queries.useQuery('get', '/v1/egress');
  const [copied, setCopied] = useState<string | null>(null);
  const ip = egress.data?.outbound_ip ?? null;

  async function copy(value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied('Copied.');
    } catch {
      setCopied('Could not copy; select the address instead.');
    }
  }

  return (
    <section className="panel tone-orange" aria-labelledby="ip-heading">
      <h2 id="ip-heading">Fixed outbound IP</h2>
      {egress.isPending ? (
        <p className="muted">Loading…</p>
      ) : egress.isError ? (
        <ProblemNotice error={egress.error} />
      ) : ip ? (
        <>
          <p>
            Your apps reach the internet from <code>{ip}</code>. Give it to a partner whose firewall
            only lets known addresses in.
          </p>
          <div className="toolbar">
            <Button onClick={() => void copy(ip)}>Copy IP</Button>
            {copied ? (
              <span className="muted" role="status">
                {copied}
              </span>
            ) : null}
          </div>
        </>
      ) : (
        <p className="muted">
          Your cell has not told us its fixed outbound IP yet. It is created when your cell is set
          up; if it stays missing, ask your operator.
        </p>
      )}
    </section>
  );
}

function AskedBy({ resource }: { readonly resource: CellResource }) {
  if (resource.cause === null) return <>Not asked for yet</>;
  return (
    <>
      {CAUSE_TEXT[resource.cause]}
      {resource.deployment_id ? (
        <>
          {' '}
          (deploy <code>{resource.deployment_id}</code>)
        </>
      ) : null}
      {resource.approval_id ? (
        <>
          {' '}
          (approval <code>{resource.approval_id}</code>)
        </>
      ) : null}
    </>
  );
}

function Resources({ resources }: { readonly resources: readonly CellResource[] }) {
  const columns: readonly Column<CellResource>[] = [
    { header: 'Part', cell: (r) => RESOURCE_TITLE[r.resource] },
    { header: 'State', cell: (r) => <Badge tone={STATE_TONE[r.state]}>{r.state}</Badge> },
    {
      header: 'Created',
      cell: (r) =>
        r.ready_at ? (
          <time dateTime={r.ready_at}>{when(r.ready_at)}</time>
        ) : r.requested_at ? (
          <>
            Asked for <time dateTime={r.requested_at}>{when(r.requested_at)}</time>
          </>
        ) : (
          'Not yet'
        ),
    },
    { header: 'Asked for by', cell: (r) => <AskedBy resource={r} /> },
    { header: 'Adds', cell: (r) => monthly(r.monthly_usd) },
  ];
  return (
    <section className="panel tone-orange" aria-labelledby="resources-heading">
      <h2 id="resources-heading">Parts created when first needed</h2>
      <p className="muted">
        Each part is created the first time an app needs it and then stays: a database for the
        first app that stores data, the egress proxy for the first allowed internet host, the data
        gateway for the first connection.
      </p>
      <Table
        caption="Parts of your cell created when first needed"
        columns={columns}
        rows={resources}
        rowKey={(r) => r.resource}
        empty="No parts."
        emptyHint="A part appears here the first time an app needs it."
      />
    </section>
  );
}

const GATEWAY_TONE: Readonly<Record<WarmGateway['state'], Tone>> = {
  off: 'neutral',
  on: 'success',
  turning_on: 'info',
  turning_off: 'info',
  failed: 'danger',
};

const GATEWAY_TEXT: Readonly<Record<WarmGateway['state'], string>> = {
  off: 'sleeps when idle',
  on: 'kept warm',
  turning_on: 'being kept warm, a few minutes',
  turning_off: 'going back to sleeping when idle',
  failed: 'could not be changed',
};

/** The saved setting, so the form starts again from it after a save. */
function savedSetting(warm: WarmOut): string {
  const named = warm.environments.filter((e) => e.warm).map((e) => e.environment_id);
  return `${named.join(',')}|${warm.gateway.warm}`;
}

function WarmPanel() {
  const { queries } = Route.useRouteContext();
  const warm = queries.useQuery('get', '/v1/warm', undefined, {
    refetchInterval: (q) =>
      q.state.data?.gateway.state.startsWith('turning') ? GATEWAY_POLL_MS : false,
  });
  return (
    <section className="panel tone-orange" aria-labelledby="warm-heading">
      <h2 id="warm-heading">Warm option</h2>
      <p className="muted">
        Every app sleeps when nobody uses it, and its first visitor waits while it wakes. A warm
        production app keeps one instance running, so it opens straight away. Preview apps are never
        warm.
      </p>
      {warm.isPending ? (
        <p className="muted">Loading…</p>
      ) : warm.isError ? (
        <ProblemNotice error={warm.error} />
      ) : (
        <WarmForm key={savedSetting(warm.data)} warm={warm.data} />
      )}
    </section>
  );
}

function WarmForm({ warm }: { readonly warm: WarmOut }) {
  const { api, queries, queryClient } = Route.useRouteContext();
  const [chosen, setChosen] = useState<ReadonlySet<string>>(
    () => new Set(warm.environments.filter((e) => e.warm).map((e) => e.environment_id)),
  );
  const [gateway, setGateway] = useState(warm.gateway.warm);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const cost = warmCost(warm, chosen.size, gateway);
  const unchanged =
    gateway === warm.gateway.warm &&
    warm.environments.every((e) => e.warm === chosen.has(e.environment_id));

  function toggle(environmentId: string, on: boolean) {
    const next = new Set(chosen);
    if (on) next.add(environmentId);
    else next.delete(environmentId);
    setChosen(next);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || unchanged) return;
    setBusy(true);
    setError(null);
    try {
      const saved = await setWarm(api, [...chosen], gateway, cost);
      queryClient.setQueryData(queries.queryOptions('get', '/v1/warm').queryKey, saved);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  if (warm.environments.length === 0) {
    return <p className="muted">No app runs in production yet.</p>;
  }
  return (
    <form className="stack" onSubmit={submit}>
      <fieldset className="choices">
        <legend>Production apps to keep warm, about ${warm.environment_monthly_usd} a month each</legend>
        {warm.environments.map((e) => (
          <label key={e.environment_id} className="check">
            <input
              type="checkbox"
              checked={chosen.has(e.environment_id)}
              onChange={(event) => toggle(e.environment_id, event.target.checked)}
            />
            <span>
              {e.app_slug}{' '}
              {e.suggested ? <Badge tone="info">suggested</Badge> : null}{' '}
              <span className="muted">
                used on {e.opened_days} of {warm.working_days} working days lately, with cold starts
                on {e.cold_start_days}
              </span>
            </span>
          </label>
        ))}
      </fieldset>
      <label className="check">
        <input type="checkbox" checked={gateway} onChange={(event) => setGateway(event.target.checked)} />
        <span>
          Keep the gateway warm too, about ${warm.gateway_monthly_usd} a month. Now it{' '}
          <Badge tone={GATEWAY_TONE[warm.gateway.state]}>{GATEWAY_TEXT[warm.gateway.state]}</Badge>
          {warm.gateway.failure_code ? (
            <>
              {' '}
              (<code>{warm.gateway.failure_code}</code>; ask your operator)
            </>
          ) : null}
        </span>
      </label>
      {chosen.size > 0 && !gateway ? (
        <div className="notice notice-warning" role="status">
          <p>
            A warm app behind a sleeping gateway still waits: every visit passes through the gateway,
            and it wakes first.
          </p>
          <Button onClick={() => setGateway(true)}>Keep the gateway warm too</Button>
        </div>
      ) : null}
      <p>
        As chosen, the warm option adds <strong>{monthly(cost)}</strong>. This is not a bill.
      </p>
      {error ? <ProblemNotice error={error} /> : null}
      <div>
        <Button type="submit" variant="primary" disabled={busy || unchanged}>
          Save warm option
        </Button>
      </div>
    </form>
  );
}

function DatabasePanel({
  database,
  environments,
}: {
  readonly database: CellDatabase;
  readonly environments: readonly CellEnvironment[];
}) {
  const withDatabase = environments.filter((e) => e.has_database);
  const offerBigger = database.nearly_full || database.tier_full_at !== null;
  const taken = database.places_used * database.connection_limit;
  const total = database.places_total * database.connection_limit;
  return (
    <section className="panel tone-orange" aria-labelledby="database-heading">
      <h2 id="database-heading">Database</h2>
      <dl className="facts">
        <dt>Tier</dt>
        <dd>
          <code>{database.tier}</code>
        </dd>
        <dt>Places used</dt>
        <dd>
          {database.places_used} of {database.places_total}
        </dd>
        <dt>Connections</dt>
        <dd>
          {database.connection_limit} for each app database, {taken} of {total} taken. Open
          connections are on each app&apos;s page.
        </dd>
      </dl>
      {offerBigger ? (
        <div className="notice notice-warning" role="status">
          {database.tier_full_at ? (
            <p>
              A deploy was refused on{' '}
              <time dateTime={database.tier_full_at}>{when(database.tier_full_at)}</time> because
              every place was taken (<code>DB_TIER_FULL</code>).
            </p>
          ) : (
            <p>
              The database is nearly full: {database.places_total - database.places_used} of{' '}
              {database.places_total} places are left.
            </p>
          )}
          <p>
            The next step is the bigger database, <code>{database.bigger_tier}</code>, at{' '}
            {monthly(database.bigger_tier_monthly_usd)}. Ask your operator to move you to it.
          </p>
        </div>
      ) : null}
      {withDatabase.length === 0 ? (
        <p className="muted">No app has a database yet.</p>
      ) : (
        <ul className="plain-list" aria-label="Apps with a database">
          {withDatabase.map((e) => (
            <li key={e.environment_id}>
              <Link to="/apps/$appId" params={{ appId: e.app_id }}>
                {e.app_slug}
              </Link>{' '}
              <span className="muted">{ENV_TITLE[e.name]}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function UsageTable({
  usage,
  environments,
}: {
  readonly usage: readonly Usage[];
  readonly environments: readonly CellEnvironment[];
}) {
  const named = new Map(environments.map((e) => [e.environment_id, e]));
  const columns: readonly Column<Usage>[] = [
    {
      header: 'App',
      cell: (u) => {
        const env = named.get(u.environment_id);
        if (!env) return <code>{u.environment_id}</code>;
        return (
          <>
            <Link to="/apps/$appId" params={{ appId: env.app_id }}>
              {env.app_slug}
            </Link>{' '}
            <span className="muted">{ENV_TITLE[env.name]}</span>
          </>
        );
      },
    },
    { header: 'Billed', cell: (u) => (u.billing ? BILLING_TEXT[u.billing] : 'No hours') },
    { header: 'Session hours', cell: (u) => hours(u.session_hours) },
    { header: 'Instance hours', cell: (u) => hours(u.instance_hours) },
    { header: 'Cold starts', cell: coldStarts },
    { header: 'Kind of use', cell: (u) => u.usage_type ?? 'None' },
  ];
  return (
    <>
      <p className="muted">
        An app sleeps when nobody uses it; a cold start is the wait while it wakes. Request-billed
        apps count only while they answer, instance-billed ones the whole time they run.
      </p>
      <Table
        caption="Usage of each app this month"
        columns={columns}
        rows={usage}
        rowKey={(u) => u.environment_id}
        empty="No app has run this month."
        emptyHint="Use appears here once someone opens an app."
      />
    </>
  );
}
