import { useInfiniteQuery } from '@tanstack/react-query';
import { createFileRoute, Link } from '@tanstack/react-router';
import { useId, useState } from 'react';
import { BILLING_TEXT, type Usage, when } from '../../api/cell';
import { must } from '../../api/client';
import {
  APP_STATUSES,
  type AppFilter,
  type AppStatus,
  type AppSummary,
  DEPLOY_TONE,
  type EnvName,
  type InventoryApp,
  type InventoryEnvironment,
  matchesFilter,
  sharingSummary,
} from '../../api/inventory';
import { ENV_TITLE } from '../../app-detail/names';
import { Badge, StatusBadge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { PageHeader } from '../../components/PageHeader';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';

export const Route = createFileRoute('/_authed/')({
  component: Inventory,
});

const ENV_NAMES: readonly EnvName[] = ['prod', 'preview'];

const EMPTY = 'No apps yet. An app appears here once it is created.';
const EMPTY_HINT = 'Ask your coding agent to create one, or create it with the ssc command line.';

function appLink(id: string, slug: string) {
  return (
    <Link to="/apps/$appId" params={{ appId: id }}>
      {slug}
    </Link>
  );
}

const COLUMNS: readonly Column<AppSummary>[] = [
  { header: 'App', cell: (app) => appLink(app.id, app.slug) },
  { header: 'Status', cell: (app) => <StatusBadge status={app.status} /> },
  { header: 'Owner', cell: (app) => <code>{app.owner_user_id}</code> },
  { header: 'App id', cell: (app) => <code>{app.id}</code> },
];

/**
 * The apps the caller may see. An org admin gets the inventory: each app's owner, last use and,
 * per environment, its release, last deploy, sharing and how it is billed. Anyone else gets the
 * plain list. Whether an app is asleep is not here: that is read live, on the app's own page.
 */
function Inventory() {
  const { queries, isAdmin } = Route.useRouteContext();
  const me = queries.useQuery('get', '/v1/whoami');
  const [text, setText] = useState('');
  const [status, setStatus] = useState<AppStatus | ''>('');
  const [env, setEnv] = useState<EnvName | ''>('');
  const textId = useId();
  const statusId = useId();
  const envId = useId();
  const admin = isAdmin(me.data);
  const filter: AppFilter = { text, status };

  return (
    <>
      <PageHeader
        title="Apps"
        purpose="The apps in your organisation. Open one to see how it is running and who can use it."
      >
        <div className="search">
          <label htmlFor={textId} className="visually-hidden">
            Filter apps
          </label>
          <input
            id={textId}
            type="search"
            placeholder="Filter by slug or owner"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <label htmlFor={statusId} className="visually-hidden">
            Status
          </label>
          <select id={statusId} value={status} onChange={(e) => setStatus(e.target.value as AppStatus | '')}>
            <option value="">Any status</option>
            {APP_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          {admin ? (
            <>
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
      </PageHeader>
      {me.isPending ? (
        <p className="muted">Loading apps…</p>
      ) : admin ? (
        <AdminInventory filter={filter} env={env} />
      ) : (
        <AppList filter={filter} />
      )}
    </>
  );
}

function countText(shown: number, all: number, more = false): string {
  if (shown !== all) return `${shown} of ${all} apps`;
  return `${all} ${all === 1 ? 'app' : 'apps'}${more ? ', more to load' : ''}`;
}

function AppList({ filter }: { readonly filter: AppFilter }) {
  const { queries } = Route.useRouteContext();
  const apps = queries.useQuery('get', '/v1/apps');
  if (apps.isPending) return <p className="muted">Loading apps…</p>;
  if (apps.isError) return <ProblemNotice error={apps.error} />;
  const all = [...apps.data.apps].sort((a, b) => a.slug.localeCompare(b.slug));
  const shown = all.filter((app) => matchesFilter({ ...app, owner: [app.owner_user_id] }, filter));
  return (
    <section className="panel">
      <Table
        caption="Apps in this organisation"
        columns={COLUMNS}
        rows={shown}
        rowKey={(app) => app.id}
        empty={all.length ? 'No app matches the filter.' : EMPTY}
        emptyHint={all.length ? 'Clear the filter to see every app.' : EMPTY_HINT}
      />
      <p className="muted count" aria-live="polite">
        {countText(shown.length, all.length)}
      </p>
    </section>
  );
}

/** One environment of one app: its release and last deploy, who it is shared with, how it is billed. */
function EnvSummary({ env, usage }: { readonly env: InventoryEnvironment | undefined; readonly usage: Usage | undefined }) {
  if (!env) return <span className="muted">None</span>;
  const deploy = env.last_deploy;
  return (
    <div className="env-summary">
      <span>
        {env.current_release ? `Release ${env.current_release.number}` : 'Not deployed yet'}
        {deploy ? (
          <>
            {' '}
            <Badge tone={DEPLOY_TONE[deploy.state]}>
              {deploy.kind === 'rollback' ? `rollback ${deploy.state}` : deploy.state}
            </Badge>
          </>
        ) : null}
      </span>
      {deploy ? (
        <span className="muted">
          Last deploy <time dateTime={deploy.at}>{when(deploy.at)}</time>
        </span>
      ) : null}
      <span>{sharingSummary(env.sharing)}</span>
      <span className="muted">{usage?.billing ? BILLING_TEXT[usage.billing] : 'Billing —'}</span>
    </div>
  );
}

function AdminInventory({ filter, env }: { readonly filter: AppFilter; readonly env: EnvName | '' }) {
  const { api, queries } = Route.useRouteContext();
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
  if (pages.isPending) return <p className="muted">Loading apps…</p>;
  // A refused first page is the whole answer; a refused later page leaves the loaded apps on show.
  if (!pages.data) return <ProblemNotice error={pages.error} />;

  const all = pages.data.pages.flatMap((p) => p.items);
  const shown = all.filter((app) =>
    matchesFilter({ slug: app.slug, status: app.status, owner: [app.owner.user_id, app.owner.display_name] }, filter),
  );
  const billed = new Map((usage.data?.environments ?? []).map((u) => [u.environment_id, u]));
  const envColumn = (name: EnvName): Column<InventoryApp> => ({
    header: ENV_TITLE[name],
    cell: (app) => {
      const found = app.environments.find((e) => e.name === name);
      return <EnvSummary env={found} usage={found ? billed.get(found.environment_id) : undefined} />;
    },
  });
  const columns: Column<InventoryApp>[] = [
    { header: 'App', cell: (app) => appLink(app.app_id, app.slug) },
    { header: 'Status', cell: (app) => <StatusBadge status={app.status} /> },
    {
      header: 'Owner',
      cell: (app) => (
        <>
          {app.owner.display_name}{' '}
          <code className="truncate muted" title={app.owner.user_id}>
            {app.owner.user_id}
          </code>
        </>
      ),
    },
    {
      header: 'Last used',
      cell: (app) =>
        app.last_used_at ? <time dateTime={app.last_used_at}>{when(app.last_used_at)}</time> : 'Not yet',
    },
    ...ENV_NAMES.filter((name) => !env || name === env).map(envColumn),
  ];

  return (
    <section className="panel">
      <Table
        dense
        caption="Apps in this organisation"
        columns={columns}
        rows={shown}
        rowKey={(app) => app.app_id}
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
    </section>
  );
}
