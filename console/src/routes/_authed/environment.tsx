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
import { AdminOnly } from '../../components/AdminOnly';
import { Badge, type Tone } from '../../components/Badge';
import { Button } from '../../components/Button';
import { Loading } from '../../components/Loading';
import { Meter } from '../../components/Meter';
import { Notice } from '../../components/Notice';
import { PageHeader } from '../../components/PageHeader';
import { Panel } from '../../components/Panel';
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
  return (
    <AdminOnly title="Usage" refusal="Only org admins can see usage.">
      <YourEnvironment />
    </AdminOnly>
  );
}

function YourEnvironment() {
  const { queries } = Route.useRouteContext();
  const cell = queries.useQuery('get', '/v1/cell');
  return (
    <>
      <PageHeader title="Usage" purpose="What your cell runs and what each part adds a month; this is not a bill." />
      {cell.isPending ? (
        <Loading label="Loading the cell…" rows={3} />
      ) : cell.isError ? (
        <ProblemNotice error={cell.error} />
      ) : (
        <>
          <PlanPanel resources={cell.data.resources} environments={cell.data.environments} />
          <WarmPanel />
          <DatabasePanel database={cell.data.database} environments={cell.data.environments} />
        </>
      )}
    </>
  );
}

/** A part adds its figure once it was asked for; one that is off, or failed, adds nothing. */
const SET_OFF: ReadonlySet<CellResource['state']> = new Set(['requested', 'creating', 'ready']);

function PlanPanel({
  resources,
  environments,
}: {
  readonly resources: readonly CellResource[];
  readonly environments: readonly CellEnvironment[];
}) {
  const { queries } = Route.useRouteContext();
  const usage = queries.useQuery('get', '/v1/usage');
  const warm = queries.useQuery('get', '/v1/warm');
  const parts = resources.filter((r) => SET_OFF.has(r.state)).reduce((sum, r) => sum + r.monthly_usd, 0);
  const total = warm.data ? parts + warm.data.monthly_usd : null;
  return (
    <Panel title="Plan and usage">
      <p>
        Added a month:{' '}
        <strong>{warm.isPending ? '…' : total === null ? 'Not available' : total === 0 ? 'nothing' : `$${total}`}</strong>
        , by the parts asked for and the warm option.
      </p>
      {usage.isPending ? (
        <Loading label="Loading usage…" rows={3} />
      ) : usage.isError ? (
        <ProblemNotice error={usage.error} />
      ) : (
        <UsageTable usage={usage.data.environments} environments={environments} />
      )}
      <details>
        <summary>Details</summary>
        <p className="muted">
          Request-billed apps count only while they answer, instance-billed ones the whole time they run.
        </p>
        {usage.data ? <UsageTable usage={usage.data.environments} environments={environments} more /> : null}
        <Resources resources={resources} />
      </details>
    </Panel>
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
          (
          <Link to="/approvals/$approvalId" params={{ approvalId: resource.approval_id }}>
            approval
          </Link>
          )
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
    <>
      <h3>Parts created when first needed</h3>
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
    </>
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
    <Panel title="Capacity">
      <p className="muted">
        Every app sleeps when nobody uses it, and its first visitor waits while it wakes. A warm
        production app keeps one instance running, so it opens straight away. Preview apps are never
        warm.
      </p>
      {warm.isPending ? (
        <Loading label="Loading the warm option…" rows={3} />
      ) : warm.isError ? (
        <ProblemNotice error={warm.error} />
      ) : (
        <WarmForm key={savedSetting(warm.data)} warm={warm.data} />
      )}
    </Panel>
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
        <Notice tone="warn">
          <p>
            A warm app behind a sleeping gateway still waits: every visit passes through the gateway,
            and it wakes first.
          </p>
          <Button onClick={() => setGateway(true)}>Keep the gateway warm too</Button>
        </Notice>
      ) : null}
      <p>
        As chosen, the warm option adds <strong>{monthly(cost)}</strong>.
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
  const left = database.places_total - database.places_used;
  return (
    <Panel title="Database">
      <dl className="facts">
        <dt>Tier</dt>
        <dd>
          <code>{database.tier}</code>
        </dd>
        <dt>Places used</dt>
        <dd>
          {database.places_used} of {database.places_total}
          <Meter
            value={database.places_used}
            max={database.places_total}
            label={`${database.places_used} of ${database.places_total} database places used`}
            warning={left <= 0 || database.nearly_full}
          />
        </dd>
        <dt>Connections</dt>
        <dd>
          {database.connection_limit} for each app database, {taken} of {total} taken. Open
          connections are on each app&apos;s page.
        </dd>
      </dl>
      {offerBigger ? (
        <Notice tone="warn">
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
          <p>
            Or give places back: deleting an app, from the admin actions on its Settings tab,
            frees the places of its databases.
          </p>
        </Notice>
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
    </Panel>
  );
}

function UsageTable({
  usage,
  environments,
  more,
}: {
  readonly usage: readonly Usage[];
  readonly environments: readonly CellEnvironment[];
  readonly more?: boolean;
}) {
  const named = new Map(environments.map((e) => [e.environment_id, e]));
  const app: Column<Usage> = {
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
  };
  const columns: readonly Column<Usage>[] = more
    ? [
        app,
        { header: 'Instance hours', cell: (u) => hours(u.instance_hours) },
        { header: 'Kind of use', cell: (u) => u.usage_type ?? 'None' },
      ]
    : [
        app,
        { header: 'Billed', cell: (u) => (u.billing ? BILLING_TEXT[u.billing] : 'No hours') },
        { header: 'Session hours', cell: (u) => hours(u.session_hours) },
        { header: 'Cold starts', cell: coldStarts },
      ];
  return (
    <Table
      caption={more ? 'More usage figures this month' : 'Usage of each app this month'}
      dense={more}
      columns={columns}
      rows={usage}
      rowKey={(u) => u.environment_id}
      empty="No app has run this month."
      emptyHint="Use appears here once someone opens an app."
    />
  );
}
