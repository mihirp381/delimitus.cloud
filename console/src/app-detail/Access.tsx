import { type Grant, grantKey } from '../api/grants';
import type { AppOut, EnvironmentOut } from '../api/lifecycle';
import { ConfirmAction } from '../components/ConfirmAction';
import { Loading } from '../components/Loading';
import { Notice } from '../components/Notice';
import { Panel } from '../components/Panel';
import { Person } from '../components/Person';
import { ProblemNotice } from '../components/ProblemNotice';
import { type Column, Table } from '../components/Table';
import { AccessExplain } from './AccessExplain';
import { EnvConnections } from './EnvConnections';
import { ENV_TITLE } from './names';
import { ShareDialog } from './ShareDialog';
import { useGrants, who } from './useGrants';

interface Props {
  readonly app: AppOut;
  readonly env: EnvironmentOut;
}

export function Access({ app, env }: Props) {
  return (
    <>
      <WhoHasAccess app={app} env={env} />
      <EnvConnections app={app} env={env} />
      <AccessExplain app={app} env={env} />
    </>
  );
}

function WhoHasAccess({ app, env }: Props) {
  const { grants, seen, waiting, applied, remove } = useGrants(app, env);
  const where = ENV_TITLE[env.name];
  const columns: readonly Column<Grant>[] = [
    { header: 'Role', cell: (g) => g.role },
    {
      header: 'Who',
      cell: (g) =>
        g.subject_kind === 'org' ? (
          who(g)
        ) : (
          <>
            {g.subject_kind === 'user' ? 'User' : 'Group'}{' '}
            <Person name={g.subject_name} email={g.subject_email} fallback={g.subject_id ?? ''} />
          </>
        ),
    },
    {
      header: 'Action',
      className: 'actions-cell',
      cell: (g) => (
        <ConfirmAction
          label="Remove access"
          accessibleLabel={`Remove access for ${who(g)} (${g.role}) in ${where}`}
          title="Remove access"
          confirmText={app.slug}
          confirmLabel="Remove access"
          onConfirm={() => remove(g)}
        >
          <p>
            {who(g)} loses the <strong>{g.role}</strong> role on {where.toLowerCase()} of{' '}
            <strong>{app.slug}</strong>.
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
          caption={`Who has access to ${where}`}
          columns={columns}
          rows={grants.data.grants}
          rowKey={grantKey}
          empty="Nobody has access yet."
          emptyHint={`Use Share to give a person, a group or everyone in the organisation access to ${where.toLowerCase()}.`}
        />
      )}
    </Panel>
  );
}
