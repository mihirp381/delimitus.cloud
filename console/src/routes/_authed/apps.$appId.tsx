import { createFileRoute, Link, redirect } from '@tanstack/react-router';
import type { AppOut, EnvironmentOut } from '../../api/lifecycle';
import { Access } from '../../app-detail/Access';
import { AdminActions } from '../../app-detail/AdminActions';
import { EnvironmentCard } from '../../app-detail/EnvironmentCard';
import { Logs } from '../../app-detail/Logs';
import { ENV_TITLE } from '../../app-detail/names';
import { Repository } from '../../app-detail/Repository';
import { Secrets } from '../../app-detail/Secrets';
import {
  APP_TAB_TITLE,
  APP_TABS,
  type AppTab,
  appEnv,
  appSearch,
  appTab,
  type EnvName,
  MOVED_TABS,
  movedTab,
  validateAppSearch,
} from '../../app-detail/tabs';
import { Timers } from '../../app-detail/Timers';
import { Ago } from '../../components/Ago';
import { StatusBadge } from '../../components/Badge';
import { Id } from '../../components/Copy';
import { Icon } from '../../components/Icon';
import { Loading } from '../../components/Loading';
import { Notice } from '../../components/Notice';
import { PageHeader } from '../../components/PageHeader';
import { Person } from '../../components/Person';
import { ProblemNotice } from '../../components/ProblemNotice';

export const Route = createFileRoute('/_authed/apps/$appId')({
  validateSearch: validateAppSearch,
  beforeLoad: ({ search, params }) => {
    const moved = movedTab(search.tab);
    if (!moved) return;
    throw redirect({
      to: '/apps/$appId',
      params,
      search: appSearch(search),
      hash: MOVED_TABS[moved].hash,
      replace: true,
    });
  },
  component: AppDetail,
});

const ENV_ORDER: Readonly<Record<EnvName, number>> = { prod: 0, preview: 1 };
const EXACT = { exact: true } as const;

function AppDetail() {
  const { appId } = Route.useParams();
  const { queries } = Route.useRouteContext();
  const app = queries.useQuery('get', '/v1/apps/{app_id}', { params: { path: { app_id: appId } } });
  if (app.isPending) return <Loading label="Loading the app…" rows={5} />;
  if (app.isError) return <ProblemNotice error={app.error} />;
  return <AppView app={app.data} />;
}

function AppView({ app }: { readonly app: AppOut }) {
  const search = Route.useSearch();
  const tab = appTab(search.tab);
  const environments = [...app.environments].sort((a, b) => ENV_ORDER[a.name] - ENV_ORDER[b.name]);
  const env = environments.find((e) => e.name === appEnv(search.env)) ?? environments[0];
  const prod = environments.find((e) => e.name === 'prod');
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
                <Person name={app.owner_name} email={app.owner_email} />{' '}
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
      <OwnerNotice app={app} />
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
            <EnvironmentCard key={e.id} app={app} env={e} prod={prod} />
          ))}
        </div>
      ) : (
        <>
          {env ? (
            <>
              <EnvSwitch app={app} tab={tab} env={env} environments={environments} />
              <EnvironmentTab key={env.id} tab={tab} app={app} env={env} />
            </>
          ) : (
            <p className="muted">This app has no environment yet.</p>
          )}
          {tab === 'settings' ? (
            <>
              <Repository app={app} />
              <AdminActions app={app} />
            </>
          ) : null}
        </>
      )}
    </>
  );
}

/** An app whose owner is no longer active; with no active admin left either, it is turned off. */
function OwnerNotice({ app }: { readonly app: AppOut }) {
  if (app.owner_state === 'active') return null;
  const blocked = app.owner_state === 'blocked';
  return (
    <Notice tone={blocked ? 'danger' : 'warn'} title="No owner. Assign one.">
      <p>
        {app.owner_name || 'Its owner'} is no longer active.{' '}
        {blocked ? 'The app is turned off, in production and preview, until an org admin assigns an owner.' : null}
        {blocked ? null : (
          <>
            An org admin assigns a new owner under{' '}
            <Link to="/apps/$appId" params={{ appId: app.id }} search={appSearch({ tab: 'settings' })}>
              Settings
            </Link>
            .
          </>
        )}
      </p>
    </Notice>
  );
}

interface SwitchProps {
  readonly app: AppOut;
  readonly tab: AppTab;
  readonly env: EnvironmentOut;
  readonly environments: readonly EnvironmentOut[];
}

function EnvSwitch({ app, tab, env, environments }: SwitchProps) {
  return (
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
  );
}

function EnvironmentTab({ tab, app, env }: { readonly tab: AppTab; readonly app: AppOut; readonly env: EnvironmentOut }) {
  if (tab === 'access') return <Access app={app} env={env} />;
  if (tab === 'logs') return <Logs app={app} env={env} />;
  if (tab === 'settings') {
    return (
      <>
        <Secrets app={app} env={env} />
        <Timers app={app} env={env} />
      </>
    );
  }
  return null;
}
