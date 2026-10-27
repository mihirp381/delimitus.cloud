import { Link, useRouteContext } from '@tanstack/react-router';
import { useEffect, useRef, useState } from 'react';
import type { Grant } from '../api/grants';
import type { AppOut, EnvironmentOut } from '../api/lifecycle';
import { Ago } from '../components/Ago';
import { Id } from '../components/Copy';
import { Loading } from '../components/Loading';
import { Notice } from '../components/Notice';
import { Panel } from '../components/Panel';
import { ENV_TITLE } from './names';
import { Promote } from './Promote';
import { Rollback } from './Rollback';
import { HealthBadge, RunningFacts, useHealth } from './Running';
import { ShareDialog } from './ShareDialog';
import { appSearch } from './tabs';
import { useGrants } from './useGrants';

const DEPLOYMENTS = '/v1/apps/{app_id}/environments/{environment_id}/deployments' as const;
const CONNECTIONS = '/v1/apps/{app_id}/environments/{environment_id}/connections' as const;
const SHOWN = 3;

/** "Everyone in the organisation, Finance team, Ana and 2 more", from an environment's grants. */
export function whoCanOpen(grants: readonly Grant[]): string {
  if (grants.length === 0) return 'Nobody yet';
  const names: string[] = [];
  const unnamed = new Set<string>();
  const seen = new Set<string>();
  for (const g of grants) {
    const key = `${g.subject_kind}:${g.subject_id ?? ''}`;
    if (seen.has(key)) continue;
    seen.add(key);
    if (g.subject_kind === 'org') names.unshift('Everyone in the organisation');
    else if (g.subject_name) names.push(g.subject_name);
    else unnamed.add(key);
  }
  const rest = Math.max(0, names.length - SHOWN) + unnamed.size;
  const shown = names.slice(0, SHOWN);
  if (shown.length === 0) return `${rest} ${rest === 1 ? 'person or group' : 'people or groups'}`;
  return rest > 0 ? `${shown.join(', ')} and ${rest} more` : shown.join(', ');
}

interface Props {
  readonly app: AppOut;
  readonly env: EnvironmentOut;
  readonly prod: EnvironmentOut | undefined;
}

export function EnvironmentCard({ app, env, prod }: Props) {
  const { queries } = useRouteContext({ from: '/_authed/apps/$appId' });
  const { grants, seen, waiting, applied } = useGrants(app, env);
  const init = { params: { path: { app_id: app.id, environment_id: env.id } } };
  const current = env.current_deployment_id;
  const deployments = queries.useQuery(
    'get',
    DEPLOYMENTS,
    { params: { ...init.params, query: { limit: 50 } } },
    { enabled: current !== null },
  );
  const linked = queries.useQuery('get', CONNECTIONS, init);
  const health = useHealth({ app, env });
  const live = deployments.data?.items.find((d) => d.current) ?? null;
  const { refetch } = deployments;
  const read = useRef(current);
  const title = ENV_TITLE[env.name];

  useEffect(() => {
    if (read.current === current) return;
    read.current = current;
    if (current !== null) void refetch();
  }, [current, refetch]);

  const count = linked.data?.connections.length;

  return (
    <Panel
      title={
        <>
          {title} <code className="chip">{env.name}</code>
        </>
      }
      actions={<HealthBadge health={health} />}
      data-env={env.name}
    >
      <dl className="facts">
        <dt>Address</dt>
        <dd>{env.url ? <a href={env.url}>{env.url}</a> : <span className="muted">None yet</span>}</dd>
        <dt>Who can open it</dt>
        <dd>
          {grants.isPending ? (
            <Loading label="Loading who has access…" inline />
          ) : grants.isError ? (
            <span className="muted">Not available</span>
          ) : (
            whoCanOpen(grants.data.grants)
          )}{' '}
          <Link
            to="/apps/$appId"
            params={{ appId: app.id }}
            search={appSearch({ tab: 'access', env: env.name })}
            aria-label={`See who has access to ${title}`}
          >
            Manage
          </Link>
        </dd>
        <dt>Last deploy</dt>
        <dd>
          {current === null ? (
            <span className="muted">None yet</span>
          ) : deployments.isPending ? (
            <Loading label="Loading the last deploy…" inline />
          ) : deployments.isError || !live ? (
            <span className="muted">Not available</span>
          ) : (
            <>
              Release {live.release_number} by {live.actor.name}, <Ago at={live.finished_at ?? live.started_at} />
            </>
          )}
        </dd>
        <dt>Data</dt>
        <dd>
          {count === undefined ? (
            <span className="muted">{linked.isError ? 'Not available' : 'Checking…'}</span>
          ) : (
            `${count} ${count === 1 ? 'connection' : 'connections'}`
          )}{' '}
          <Link
            to="/apps/$appId"
            params={{ appId: app.id }}
            search={appSearch({ tab: 'access', env: env.name })}
            hash="data-connections"
            aria-label={`Data connections of ${title}`}
          >
            Manage
          </Link>
        </dd>
      </dl>
      <div className="toolbar">
        {env.name === 'prod' ? (
          <ShareDialog app={app} env={env} seen={seen} onDone={(u, m) => void applied(u, m)} />
        ) : null}
        {env.name === 'preview' && prod ? <Promote app={app} prod={prod} preview={env} /> : null}
        <Rollback app={app} env={env} />
      </div>
      {waiting ? (
        <Notice tone="info">
          <p>{waiting}</p>
        </Notice>
      ) : null}
      <Details
        app={app}
        env={env}
        release={live?.release_number ?? null}
        sharing={grants.data?.grants_version}
        asleep={health.data?.state === 'asleep'}
      />
    </Panel>
  );
}

interface DetailsProps {
  readonly app: AppOut;
  readonly env: EnvironmentOut;
  readonly release: number | null;
  readonly sharing: number | undefined;
  readonly asleep: boolean;
}

function Details({ app, env, release, sharing, asleep }: DetailsProps) {
  const [open, setOpen] = useState(false);
  const current = env.current_deployment_id;
  return (
    <details onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary aria-label={`Details of ${ENV_TITLE[env.name]}`}>Details</summary>
      {open ? (
        <dl className="facts">
          <dt>Release</dt>
          <dd className="num">{release === null ? <span className="muted">None</span> : `Release ${release}`}</dd>
          <dt>Deployment</dt>
          <dd>
            {current ? <Id value={current} label={`${ENV_TITLE[env.name].toLowerCase()} deployment id`} /> : 'None yet'}
          </dd>
          <dt>Environment</dt>
          <dd>
            <Id value={env.id} label={`${ENV_TITLE[env.name].toLowerCase()} environment id`} />
          </dd>
          <dt>Config version</dt>
          <dd className="num">{env.config_version}</dd>
          <dt>Sharing version</dt>
          <dd className="num">{sharing ?? env.grants_version}</dd>
          <RunningFacts app={app} env={env} asleep={asleep} />
        </dl>
      ) : null}
    </details>
  );
}
