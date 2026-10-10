import { createFileRoute, Link } from '@tanstack/react-router';
import { useState } from 'react';
import {
  etagOf,
  type Grant,
  grantKey,
  type GrantsUpdate,
  updateGrants,
  withoutGrant,
} from '../../api/grants';
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
import { Timers } from '../../app-detail/Timers';
import { StatusBadge } from '../../components/Badge';
import { ConfirmAction } from '../../components/ConfirmAction';
import { PageHeader } from '../../components/PageHeader';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';

export const Route = createFileRoute('/_authed/apps/$appId')({
  component: AppDetail,
});

const ENV_ORDER: Readonly<Record<EnvironmentOut['name'], number>> = { prod: 0, preview: 1 };

function who(g: Grant): string {
  if (g.subject_kind === 'org') return 'Everyone in the organisation';
  return `${g.subject_kind === 'user' ? 'User' : 'Group'} ${g.subject_id ?? ''}`;
}

function AppDetail() {
  const { appId } = Route.useParams();
  const { queries } = Route.useRouteContext();
  const app = queries.useQuery('get', '/v1/apps/{app_id}', { params: { path: { app_id: appId } } });

  return (
    <>
      <p className="crumbs">
        <Link to="/">All apps</Link>
      </p>
      {app.isPending ? (
        <p className="muted">Loading the app…</p>
      ) : app.isError ? (
        <ProblemNotice error={app.error} />
      ) : (
        <AppView app={app.data} />
      )}
    </>
  );
}

function AppView({ app }: { readonly app: AppOut }) {
  const environments = [...app.environments].sort((a, b) => ENV_ORDER[a.name] - ENV_ORDER[b.name]);
  return (
    <>
      <PageHeader title={app.slug} status={<StatusBadge status={app.status} />} />
      <section className="panel">
        <dl className="facts">
          <dt>Owner</dt>
          <dd>
            <code>{app.owner_user_id}</code>
          </dd>
          <dt>Created</dt>
          <dd>
            <time dateTime={app.created_at}>{new Date(app.created_at).toLocaleString()}</time>
          </dd>
          <dt>App id</dt>
          <dd>
            <code>{app.id}</code>
          </dd>
        </dl>
      </section>
      {environments.map((env) => (
        <EnvironmentPanel key={env.id} app={app} env={env} />
      ))}
      <Repository app={app} />
      <AdminActions app={app} />
    </>
  );
}

function EnvironmentPanel({ app, env }: { readonly app: AppOut; readonly env: EnvironmentOut }) {
  const { api, queries, queryClient } = Route.useRouteContext();
  const target = { appId: app.id, environmentId: env.id };
  const init = { params: { path: { app_id: app.id, environment_id: env.id } } };
  const path = '/v1/apps/{app_id}/environments/{environment_id}/grants' as const;
  const grants = queries.useQuery('get', path, init);
  const [notice, setNotice] = useState<string | null>(null);
  const headingId = `env-${env.id}`;
  const seen = grants.data ? { grants: grants.data, etag: etagOf(grants.data) } : undefined;
  const preview = app.environments.find((e) => e.name === 'preview');

  async function applied(updated: GrantsUpdate, message: string) {
    queryClient.setQueryData(queries.queryOptions('get', path, init).queryKey, updated.grants);
    setNotice(message);
    if (updated.state === 'pending') return;
    await queryClient.invalidateQueries({
      queryKey: queries.queryOptions('get', '/v1/apps/{app_id}', {
        params: { path: { app_id: app.id } },
      }).queryKey,
    });
  }

  async function remove(grant: Grant) {
    setNotice(null);
    const updated = await updateGrants(api, target, withoutGrant(grant), seen);
    await applied(
      updated,
      updated.state === 'pending'
        ? `Waiting for approval, nothing changed yet: ${updated.approvalIds.join(', ')}.`
        : `Removed access for ${who(grant)} (${grant.role}).`,
    );
  }

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
    <section className={env.name === 'prod' ? 'panel' : 'panel tone-violet'} aria-labelledby={headingId}>
      <h2 id={headingId}>
        {ENV_TITLE[env.name]} <code className="chip">{env.name}</code>
      </h2>
      <dl className="facts">
        <Running app={app} env={env} />
        <dt>Current deployment</dt>
        <dd>{env.current_deployment_id ? <code>{env.current_deployment_id}</code> : 'None yet'}</dd>
        <dt>Config version</dt>
        <dd>{env.config_version}</dd>
        <dt>Sharing version</dt>
        <dd>{grants.data?.grants_version ?? env.grants_version}</dd>
        {env.url ? (
          <>
            <dt>Address</dt>
            <dd>
              <a href={env.url}>{env.url}</a>
            </dd>
          </>
        ) : null}
      </dl>
      <div className="toolbar">
        <ShareDialog app={app} env={env} seen={seen} onDone={(u, m) => void applied(u, m)} />
        <Rollback app={app} env={env} />
        {env.name === 'prod' && preview ? <Promote app={app} prod={env} preview={preview} /> : null}
      </div>
      <h3>Who has access</h3>
      {notice ? (
        <p className="notice notice-success" role="status">
          {notice}
        </p>
      ) : null}
      {grants.isPending ? (
        <p className="muted">Loading access…</p>
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
      <AccessExplain app={app} env={env} />
      <EnvConnections app={app} env={env} />
      <Secrets app={app} env={env} />
      <Timers app={app} env={env} />
      <Logs app={app} env={env} />
    </section>
  );
}
