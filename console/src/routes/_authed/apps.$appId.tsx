import { createFileRoute, Link } from '@tanstack/react-router';
import { useState } from 'react';
import { etagOf, type Grant, grantKey, updateGrants, withoutGrant } from '../../api/grants';
import type { components } from '../../api/schema';
import { StatusBadge } from '../../components/Badge';
import { ConfirmAction } from '../../components/ConfirmAction';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';

type AppOut = components['schemas']['AppOut'];
type EnvironmentOut = components['schemas']['EnvironmentOut'];

export const Route = createFileRoute('/_authed/apps/$appId')({
  component: AppDetail,
});

const ENV_ORDER: Readonly<Record<EnvironmentOut['name'], number>> = { prod: 0, preview: 1 };
const ENV_TITLE: Readonly<Record<EnvironmentOut['name'], string>> = {
  prod: 'Production',
  preview: 'Preview',
};

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
      <div className="page-head">
        <h1>{app.slug}</h1>
        <StatusBadge status={app.status} />
      </div>
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

  async function remove(grant: Grant) {
    setNotice(null);
    const seen = grants.data ? { grants: grants.data, etag: etagOf(grants.data) } : undefined;
    const updated = await updateGrants(api, target, withoutGrant(grant), seen);
    queryClient.setQueryData(queries.queryOptions('get', path, init).queryKey, updated);
    await queryClient.invalidateQueries({
      queryKey: queries.queryOptions('get', '/v1/apps/{app_id}', {
        params: { path: { app_id: app.id } },
      }).queryKey,
    });
    setNotice(`Removed access for ${who(grant)} (${grant.role}).`);
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
    <section className="panel" aria-labelledby={headingId}>
      <h2 id={headingId}>
        {ENV_TITLE[env.name]} <code className="muted">{env.name}</code>
      </h2>
      <dl className="facts">
        <dt>Current deployment</dt>
        <dd>{env.current_deployment_id ? <code>{env.current_deployment_id}</code> : 'None yet'}</dd>
        <dt>Config version</dt>
        <dd>{env.config_version}</dd>
        <dt>Sharing version</dt>
        <dd>{grants.data?.grants_version ?? env.grants_version}</dd>
      </dl>
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
        />
      )}
    </section>
  );
}
