import { createFileRoute, Link } from '@tanstack/react-router';
import { useEffect, useRef } from 'react';
import { type Grant, grantKey } from '../../api/grants';
import type { AppOut, EnvironmentOut } from '../../api/lifecycle';
import { AccessExplain } from '../../app-detail/AccessExplain';
import { AdminActions } from '../../app-detail/AdminActions';
import { EnvConnections } from '../../app-detail/EnvConnections';
import { Logs } from '../../app-detail/Logs';
import { ENV_TITLE } from '../../app-detail/names';
import { Promote } from '../../app-detail/Promote';
import { Repository } from '../../app-detail/Repository';
import { Rollback } from '../../app-detail/Rollback';
import { Running } from '../../app-detail/Running';
import { Secrets } from '../../app-detail/Secrets';
import { ShareDialog } from '../../app-detail/ShareDialog';
import {
  APP_TAB_TITLE,
  APP_TABS,
  type AppTab,
  appEnv,
  appSearch,
  appTab,
  ENV_TABS,
  type EnvName,
} from '../../app-detail/tabs';
import { Timers } from '../../app-detail/Timers';
import { useGrants, who } from '../../app-detail/useGrants';
import { Ago } from '../../components/Ago';
import { StatusBadge } from '../../components/Badge';
import { ConfirmAction } from '../../components/ConfirmAction';
import { Id } from '../../components/Copy';
import { Icon } from '../../components/Icon';
import { Loading } from '../../components/Loading';
import { Notice } from '../../components/Notice';
import { PageHeader } from '../../components/PageHeader';
import { Panel } from '../../components/Panel';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';

export const Route = createFileRoute('/_authed/apps/$appId')({
  validateSearch: appSearch,
  component: AppDetail,
});

const ENV_ORDER: Readonly<Record<EnvName, number>> = { prod: 0, preview: 1 };
const EXACT = { exact: true } as const;
const DEPLOYMENTS = '/v1/apps/{app_id}/environments/{environment_id}/deployments' as const;

function AppDetail() {
  const { appId } = Route.useParams();
  const { queries } = Route.useRouteContext();
  const app = queries.useQuery('get', '/v1/apps/{app_id}', { params: { path: { app_id: appId } } });

  return (
    <>
      {app.isPending ? (
        <Loading label="Loading the app…" rows={5} />
      ) : app.isError ? (
        <ProblemNotice error={app.error} />
      ) : (
        <AppView app={app.data} />
      )}
    </>
  );
}

/**
 * One app: who owns it and what it is called, then one tab at a time. The tab and, on the tabs
 * that show one environment, the environment are held in the address, so a tab can be linked to
 * and the back button steps through them. An address without either lands on the overview.
 */
function AppView({ app }: { readonly app: AppOut }) {
  // Read through the same check as the route's: a match's search keeps what the address said
  // beside what the check let through, so a tab it does not know is still there to be ignored.
  const search = Route.useSearch();
  const tab = appTab(search.tab);
  const environments = [...app.environments].sort((a, b) => ENV_ORDER[a.name] - ENV_ORDER[b.name]);
  const env = environments.find((e) => e.name === appEnv(search.env)) ?? environments[0];
  const preview = environments.find((e) => e.name === 'preview');
  const opens = environments.filter((e) => e.url);

  return (
    <>
      <PageHeader
        title={app.slug}
        status={<StatusBadge status={app.status} />}
        meta={
          <dl className="meta">
            <div>
              <dt>Owner</dt>
              <dd>
                <Id value={app.owner_user_id} label="owner id" />
              </dd>
            </div>
            <div>
              <dt>App id</dt>
              <dd>
                <Id value={app.id} label="app id" />
              </dd>
            </div>
            <div>
              <dt>Created</dt>
              <dd>
                <Ago at={app.created_at} />
              </dd>
            </div>
          </dl>
        }
      >
        {opens.map((e) => (
          <a
            key={e.id}
            className={e.name === 'prod' ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
            href={e.url ?? undefined}
          >
            Open {ENV_TITLE[e.name].toLowerCase()}
            <Icon name="external" />
          </a>
        ))}
      </PageHeader>
      <nav className="tabs" aria-label="App sections">
        {APP_TABS.map((t) => (
          <Link
            key={t}
            to="/apps/$appId"
            params={{ appId: app.id }}
            search={appSearch({ tab: t, env: env?.name })}
            activeOptions={EXACT}
            className={t === tab ? 'current' : undefined}
            aria-current={t === tab ? 'page' : undefined}
          >
            {APP_TAB_TITLE[t]}
          </Link>
        ))}
      </nav>
      {tab === 'overview' ? (
        <div className="cards">
          {environments.map((e) => (
            <EnvironmentCard key={e.id} app={app} env={e} preview={preview} />
          ))}
        </div>
      ) : tab === 'settings' ? (
        <>
          <Repository app={app} />
          <AdminActions app={app} />
        </>
      ) : env ? (
        <>
          <div className="tab-bar">
            <nav className="segmented" aria-label="Environment">
              {environments.map((e) => (
                <Link
                  key={e.id}
                  to="/apps/$appId"
                  params={{ appId: app.id }}
                  search={appSearch({ tab, env: e.name })}
                  activeOptions={EXACT}
                  className={e.id === env.id ? 'segment current' : 'segment'}
                  aria-current={e.id === env.id ? 'true' : undefined}
                >
                  {ENV_TITLE[e.name]}
                </Link>
              ))}
            </nav>
            {env.url ? (
              <a className="muted" href={env.url}>
                {env.url}
              </a>
            ) : null}
          </div>
          <EnvironmentTab key={env.id} tab={tab} app={app} env={env} />
        </>
      ) : (
        <p className="muted">This app has no environment yet.</p>
      )}
    </>
  );
}

/** What one of the per-environment tabs shows for the environment the switch is on. */
function EnvironmentTab({ tab, app, env }: { readonly tab: AppTab; readonly app: AppOut; readonly env: EnvironmentOut }) {
  if (!ENV_TABS.has(tab)) return null;
  if (tab === 'access') {
    return (
      <>
        <Access app={app} env={env} />
        <AccessExplain app={app} env={env} />
      </>
    );
  }
  if (tab === 'data') return <EnvConnections app={app} env={env} />;
  if (tab === 'secrets') return <Secrets app={app} env={env} />;
  if (tab === 'timers') return <Timers app={app} env={env} />;
  return <Logs app={app} env={env} />;
}

/** "Everyone in the organisation, 2 people and 1 group", from an environment's sharing rules. */
function sharedWith(grants: readonly Grant[]): string {
  if (grants.length === 0) return 'Nobody yet';
  const people = new Set(grants.filter((g) => g.subject_kind === 'user').map((g) => g.subject_id)).size;
  const groups = new Set(grants.filter((g) => g.subject_kind === 'group').map((g) => g.subject_id)).size;
  const parts = [
    ...(grants.some((g) => g.subject_kind === 'org') ? ['Everyone in the organisation'] : []),
    ...(people > 0 ? [`${people} ${people === 1 ? 'person' : 'people'}`] : []),
    ...(groups > 0 ? [`${groups} ${groups === 1 ? 'group' : 'groups'}`] : []),
  ];
  return parts.join(', ');
}

interface CardProps {
  readonly app: AppOut;
  readonly env: EnvironmentOut;
  readonly preview: EnvironmentOut | undefined;
}

/**
 * One environment on the overview: how it is running, what it runs and where, who it is shared
 * with, and the three things done to an environment as a whole: share it, roll it back and, for
 * production, promote preview to it.
 */
function EnvironmentCard({ app, env, preview }: CardProps) {
  const { queries } = Route.useRouteContext();
  const { grants, seen, waiting, applied } = useGrants(app, env);
  const current = env.current_deployment_id;
  const deployments = queries.useQuery(
    'get',
    DEPLOYMENTS,
    { params: { path: { app_id: app.id, environment_id: env.id }, query: { limit: 50 } } },
    { enabled: current !== null },
  );
  const live = deployments.data?.items.find((d) => d.current) ?? null;
  const { refetch } = deployments;

  const read = useRef(current);

  // A deploy, a rollback or a promote moved the environment on: read what it runs again.
  useEffect(() => {
    if (read.current === current) return;
    read.current = current;
    if (current !== null) void refetch();
  }, [current, refetch]);

  return (
    <Panel
      title={
        <>
          {ENV_TITLE[env.name]} <code className="chip">{env.name}</code>
        </>
      }
      data-env={env.name}
    >
      <dl className="facts">
        <Running app={app} env={env}>
          {current ? (
            <>
              <dt>Release</dt>
              <dd className="num">
                {deployments.isPending ? (
                  <Loading label="Loading the release…" inline />
                ) : deployments.isError || !live ? (
                  <span className="muted">Not available</span>
                ) : (
                  `Release ${live.release_number}`
                )}
              </dd>
            </>
          ) : null}
          <dt>Deployment</dt>
          <dd>{current ? <Id value={current} label={`${ENV_TITLE[env.name].toLowerCase()} deployment id`} /> : 'None yet'}</dd>
          {env.url ? (
            <>
              <dt>Address</dt>
              <dd>
                <a href={env.url}>{env.url}</a>
              </dd>
            </>
          ) : null}
        </Running>
        <dt>Shared with</dt>
        <dd>
          {grants.isPending ? (
            <Loading label="Loading who has access…" inline />
          ) : grants.isError ? (
            <span className="muted">Not available</span>
          ) : (
            sharedWith(grants.data.grants)
          )}{' '}
          <Link
            to="/apps/$appId"
            params={{ appId: app.id }}
            search={appSearch({ tab: 'access', env: env.name })}
            aria-label={`See who has access to ${ENV_TITLE[env.name]}`}
          >
            See who
          </Link>
        </dd>
        <dt>Config version</dt>
        <dd className="num">{env.config_version}</dd>
        <dt>Sharing version</dt>
        <dd className="num">{grants.data?.grants_version ?? env.grants_version}</dd>
      </dl>
      <div className="toolbar">
        <ShareDialog app={app} env={env} seen={seen} onDone={(u, m) => void applied(u, m)} />
        <Rollback app={app} env={env} />
        {env.name === 'prod' && preview ? <Promote app={app} prod={env} preview={preview} /> : null}
      </div>
      {waiting ? (
        <Notice tone="info">
          <p>{waiting}</p>
        </Notice>
      ) : null}
    </Panel>
  );
}

/** Who has access to one environment, each rule with the button that removes it. */
function Access({ app, env }: { readonly app: AppOut; readonly env: EnvironmentOut }) {
  const { grants, seen, waiting, applied, remove } = useGrants(app, env);
  const columns: readonly Column<Grant>[] = [
    { header: 'Role', cell: (g) => g.role },
    {
      header: 'Who',
      cell: (g) =>
        g.subject_kind === 'org' ? (
          who(g)
        ) : (
          <>
            {g.subject_kind === 'user' ? 'User' : 'Group'} <code>{g.subject_id}</code>
          </>
        ),
    },
    {
      header: 'Action',
      className: 'actions-cell',
      cell: (g) => (
        <ConfirmAction
          label="Remove access"
          accessibleLabel={`Remove access for ${who(g)} (${g.role}) in ${ENV_TITLE[env.name]}`}
          title="Remove access"
          confirmText={app.slug}
          confirmLabel="Remove access"
          onConfirm={() => remove(g)}
        >
          <p>
            {who(g)} loses the <strong>{g.role}</strong> role on {ENV_TITLE[env.name].toLowerCase()}{' '}
            of <strong>{app.slug}</strong>.
          </p>
        </ConfirmAction>
      ),
    },
  ];

  return (
    <Panel
      title="Who has access"
      actions={<ShareDialog app={app} env={env} seen={seen} onDone={(u, m) => void applied(u, m)} />}
    >
      {waiting ? (
        <Notice tone="info">
          <p>{waiting}</p>
        </Notice>
      ) : null}
      {grants.isPending ? (
        <Loading label="Loading access…" rows={3} />
      ) : grants.isError ? (
        <ProblemNotice error={grants.error} />
      ) : (
        <Table
          caption={`Who has access to ${ENV_TITLE[env.name]}`}
          columns={columns}
          rows={grants.data.grants}
          rowKey={grantKey}
          empty="Nobody has access yet."
          emptyHint={`Use Share to give a person, a group or everyone in the organisation access to ${ENV_TITLE[env.name].toLowerCase()}.`}
        />
      )}
    </Panel>
  );
}
