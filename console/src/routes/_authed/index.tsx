import { useInfiniteQuery } from '@tanstack/react-query';
import { createFileRoute, Link, useNavigate } from '@tanstack/react-router';
import { useId, useState } from 'react';
import { BILLING_TEXT, type Usage, when } from '../../api/cell';
import { must } from '../../api/client';
import {
  type AppFilter,
  type AppStatus,
  countText,
  DEPLOY_TONE,
  type EnvName,
  type InventoryApp,
  type InventoryEnvironment,
  matchesFilter,
  sharingSummary,
} from '../../api/inventory';
import { ENV_TITLE } from '../../app-detail/names';
import { OpenApps } from '../../apps/OpenApps';
import { WhoCanUse } from '../../apps/WhoCanUse';
import { Ago } from '../../components/Ago';
import { Badge, STATUS_TONE, StatusBadge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { Person } from '../../components/Person';
import { Loading } from '../../components/Loading';
import { PageHeader } from '../../components/PageHeader';
import { ProblemNotice } from '../../components/ProblemNotice';
import { Segmented } from '../../components/Segmented';
import { type Column, Table } from '../../components/Table';

export const Route = createFileRoute('/_authed/')({
  component: Inventory,
});

const ENV_NAMES: readonly EnvName[] = ['prod', 'preview'];

const EMPTY = 'No apps yet. An app appears here once it is created.';
const EMPTY_HINT = 'Ask your coding agent to create one, or create it with the ssc command line.';
const NONE_SHARED = 'No apps are shared with you yet.';
const NONE_SHARED_HINT = "Ask the app's owner or an org admin to share one with you.";
const ADMIN_NONE_SHARED = 'Admins see every app here but open only the ones shared with them, like everyone else.';

type View = 'all' | 'mine';
const VIEW_OPTIONS: readonly { value: View; label: string }[] = [
  { value: 'all', label: 'All apps' },
  { value: 'mine', label: 'Apps I can open' },
];

// Keyed by the generated union: a status the API adds or removes fails the typecheck.
const STATUS_CHOICE: Readonly<Record<AppStatus | '', string>> = {
  '': 'All',
  active: 'Active',
  disabled: 'Disabled',
  quarantined: 'Quarantined',
  deleting: 'Deleting',
};
const STATUS_OPTIONS = (Object.keys(STATUS_CHOICE) as (AppStatus | '')[]).map((value) => ({
  value,
  label: STATUS_CHOICE[value],
}));

/**
 * An app in a list: a dot for its status, then its slug, which is the link that opens it. An app
 * that is not active says so in words as well; for an active one the dot's name carries it.
 */
function AppName({ id, slug, status }: { readonly id: string; readonly slug: string; readonly status: AppStatus }) {
  const active = status === 'active';
  return (
    <span className="app-name">
      <span
        className={`dot dot-${STATUS_TONE[status] ?? 'warning'}`}
        role={active ? 'img' : undefined}
        aria-label={active ? status : undefined}
        aria-hidden={active ? undefined : true}
        title={status}
      />
      <Link to="/apps/$appId" params={{ appId: id }}>
        {slug}
      </Link>
      {active ? null : <StatusBadge status={status} />}
    </span>
  );
}

/**
 * An org admin gets the inventory (each app's owner, what each environment runs, when it was last
 * used and who it is shared with) or, as anyone else gets, the apps they may open themselves.
 * Whether an app is asleep is not here: that is read live, on the app's own page.
 */
function Inventory() {
  const { queries, isAdmin } = Route.useRouteContext();
  const me = queries.useQuery('get', '/v1/whoami');
  const [text, setText] = useState('');
  const [status, setStatus] = useState<AppStatus | ''>('');
  const [env, setEnv] = useState<EnvName | ''>('');
  const [view, setView] = useState<View>('all');
  const textId = useId();
  const envId = useId();
  const admin = isAdmin(me.data);
  const member = me.data !== undefined && !admin;
  const inventory = admin && view === 'all';
  const filter: AppFilter = { text, status };

  return (
    <>
      <PageHeader
        title={member ? 'Your apps' : 'Apps'}
        purpose={
          member
            ? 'The apps shared with you: open one in production or preview.'
            : 'The apps in your organisation: open one to see how it is running and who can use it.'
        }
        about={
          member ? (
            <p>
              An app is listed once its owner or an org admin shares it with you, with a group you
              are in or with everyone. As a builder you can also open its page to deploy and share it.
            </p>
          ) : (
            <p>
              All apps is the whole organisation. Apps I can open is what the sharing rules let you
              open yourself: being an admin opens no app.
            </p>
          )
        }
      />
      {admin ? (
        <div className="toolbar view-switch">
          <Segmented label="Show" options={VIEW_OPTIONS} value={view} onChange={setView} />
        </div>
      ) : null}
      <div className="search">
        <label htmlFor={textId} className="visually-hidden">
          Filter apps
        </label>
        <input
          id={textId}
          type="search"
          placeholder={inventory ? 'Filter by slug or owner' : 'Filter by slug or access'}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        {inventory ? (
          <>
            <Segmented label="Status" options={STATUS_OPTIONS} value={status} onChange={setStatus} />
            <span className="spacer" />
            <label htmlFor={envId} className="visually-hidden">
              Environment
            </label>
            <select id={envId} value={env} onChange={(e) => setEnv(e.target.value as EnvName | '')}>
              <option value="">Both environments</option>
              {ENV_NAMES.map((name) => (
                <option key={name} value={name}>
                  {ENV_TITLE[name]}
                </option>
              ))}
            </select>
          </>
        ) : null}
      </div>
      {me.isPending ? (
        <Loading label="Loading apps…" />
      ) : inventory ? (
        <AdminInventory filter={filter} env={env} />
      ) : (
        <OpenApps
          text={text}
          empty={admin ? ADMIN_NONE_SHARED : NONE_SHARED}
          emptyHint={admin ? undefined : NONE_SHARED_HINT}
        />
      )}
    </>
  );
}

/**
 * One environment of one app, as a chip: its release, in the tone of its last deploy. A deploy
 * that is not healthy says so in words. When it was deployed and how it is billed are in the
 * chip's title, and read out with it.
 */
function EnvChip({ env, usage }: { readonly env: InventoryEnvironment | undefined; readonly usage: Usage | undefined }) {
  if (!env) return <span className="muted">None</span>;
  const deploy = env.last_deploy;
  const billing = usage?.billing ? BILLING_TEXT[usage.billing] : 'Billing —';
  if (!env.current_release && !deploy) {
    return (
      <span className="muted" title={billing}>
        Not deployed yet<span className="visually-hidden">. {billing}</span>
      </span>
    );
  }
  const state = deploy ? (deploy.kind === 'rollback' ? `rollback ${deploy.state}` : deploy.state) : null;
  const quiet = deploy?.state === 'healthy' && deploy.kind !== 'rollback';
  const deployed = deploy ? `Last deploy ${when(deploy.at)}` : null;
  return (
    <span className="env-chip" title={[deployed, billing].filter(Boolean).join(' · ')}>
      <Badge tone={deploy ? DEPLOY_TONE[deploy.state] : 'neutral'}>
        <span className="dot" aria-hidden="true" />
        {env.current_release ? `Release ${env.current_release.number}` : 'Not deployed yet'}
        {state ? <span className={quiet ? 'visually-hidden' : undefined}> {state}</span> : null}
      </Badge>
      <span className="visually-hidden">
        {deploy ? (
          <>
            . Last deploy <time dateTime={deploy.at}>{when(deploy.at)}</time>
          </>
        ) : null}
        . {billing}
      </span>
    </span>
  );
}

/**
 * Who an app is shared with, in a line, as the button that opens who can use it. With both
 * environments on show it is production's, the one people use; preview's is in the title and
 * read out after it.
 */
function Access({
  app,
  names,
  onOpen,
}: {
  readonly app: InventoryApp;
  readonly names: readonly EnvName[];
  readonly onOpen: (app: InventoryApp) => void;
}) {
  const found = names.flatMap((name) => app.environments.filter((e) => e.name === name));
  const [first, ...rest] = found;
  if (!first) return <span className="muted">None</span>;
  const line = (e: InventoryEnvironment) => `${ENV_TITLE[e.name]}: ${sharingSummary(e.sharing)}`;
  return (
    <button type="button" className="link-btn access-btn" aria-haspopup="dialog" onClick={() => onOpen(app)}>
      <span className="clip" title={found.map(line).join(' · ')}>
        {sharingSummary(first.sharing)}
        {rest.map((e) => (
          <span key={e.environment_id} className="visually-hidden">
            ; {line(e)}
          </span>
        ))}
      </span>
    </button>
  );
}

function AdminInventory({ filter, env }: { readonly filter: AppFilter; readonly env: EnvName | '' }) {
  const { api, queries } = Route.useRouteContext();
  const navigate = useNavigate();
  const [who, setWho] = useState<InventoryApp | null>(null);
  const pages = useInfiniteQuery({
    queryKey: ['inventory'],
    queryFn: async ({ pageParam, signal }) =>
      must(
        await api.GET('/v1/inventory', {
          params: { query: pageParam === null ? {} : { cursor: pageParam } },
          signal,
        }),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
  });
  // How each environment was billed this month. The list does not wait for it or fail with it.
  const usage = queries.useQuery('get', '/v1/usage', {}, { retry: false });
  if (pages.isPending) return <Loading label="Loading apps…" rows={6} />;
  // A refused first page is the whole answer; a refused later page leaves the loaded apps on show.
  if (!pages.data) return <ProblemNotice error={pages.error} />;

  const all = pages.data.pages.flatMap((p) => p.items);
  const shown = all.filter((app) =>
    matchesFilter({ slug: app.slug, status: app.status, owner: [app.owner.name, app.owner.email ?? '', app.owner.user_id] }, filter),
  );
  const billed = new Map((usage.data?.environments ?? []).map((u) => [u.environment_id, u]));
  const names = ENV_NAMES.filter((name) => !env || name === env);
  const envColumn = (name: EnvName): Column<InventoryApp> => ({
    header: ENV_TITLE[name],
    cell: (app) => {
      const found = app.environments.find((e) => e.name === name);
      return <EnvChip env={found} usage={found ? billed.get(found.environment_id) : undefined} />;
    },
  });
  const columns: Column<InventoryApp>[] = [
    { header: 'App', cell: (app) => <AppName id={app.app_id} slug={app.slug} status={app.status} /> },
    {
      header: 'Owner',
      cell: (app) => (
        <>
          <Person name={app.owner.name} email={app.owner.email} />
          {app.owner_state === 'active' ? null : (
            <>
              {' '}
              <Badge tone="warning">No owner. Assign one.</Badge>
            </>
          )}
        </>
      ),
    },
    ...names.map(envColumn),
    {
      header: 'Last used',
      className: 'nowrap',
      cell: (app) => (app.last_used_at ? <Ago at={app.last_used_at} /> : <span className="muted">Not yet</span>),
    },
    { header: 'Access', className: 'fill', cell: (app) => <Access app={app} names={names} onOpen={setWho} /> },
  ];

  return (
    <section className="panel">
      <Table
        caption="Apps in this organisation"
        columns={columns}
        rows={shown}
        rowKey={(app) => app.app_id}
        onOpen={(app) => void navigate({ to: '/apps/$appId', params: { appId: app.app_id } })}
        empty={all.length ? 'No app matches the filter.' : EMPTY}
        emptyHint={
          all.length
            ? pages.hasNextPage
              ? 'Clear the filter, or load more apps: the filter looks only at the apps loaded so far.'
              : 'Clear the filter to see every app.'
            : EMPTY_HINT
        }
      />
      <div className="more">
        <p className="muted count" aria-live="polite">
          {countText(shown.length, all.length, pages.hasNextPage)}
        </p>
        {pages.hasNextPage ? (
          <Button onClick={() => void pages.fetchNextPage()} disabled={pages.isFetchingNextPage}>
            {pages.isFetchingNextPage ? 'Loading…' : 'Load more apps'}
          </Button>
        ) : null}
      </div>
      {pages.isError ? <ProblemNotice error={pages.error} /> : null}
      <WhoCanUse app={who} onClose={() => setWho(null)} />
    </section>
  );
}
