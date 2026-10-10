import { Link, useRouteContext } from '@tanstack/react-router';
import { type FormEvent, useId, useState } from 'react';
import {
  askCeilingApproval,
  attachConnection,
  type ConnectionSchema,
  detachConnection,
  type EnvironmentConnection,
  type EnvironmentConnections,
  limitPhrases,
} from '../api/connections';
import type { Approval } from '../api/grants';
import type { AppOut, EnvironmentOut } from '../api/lifecycle';
import { ApiProblem } from '../api/problem';
import { Badge } from '../components/Badge';
import { Button } from '../components/Button';
import { ConfirmAction } from '../components/ConfirmAction';
import { Phrases } from '../components/Phrases';
import { ProblemNotice } from '../components/ProblemNotice';
import { type Column, Table } from '../components/Table';
import { ENV_TITLE } from './names';

interface Props {
  readonly app: AppOut;
  readonly env: EnvironmentOut;
}

const PATH = '/v1/apps/{app_id}/environments/{environment_id}/connections' as const;
const SCHEMA_PATH = '/v1/apps/{app_id}/environments/{environment_id}/connections/{name}/schema' as const;

/**
 * The data connections one environment may reach, read only once the section is opened. Org
 * admins link and unlink them; everyone else sees the list.
 */
export function EnvConnections({ app, env }: Props) {
  const [open, setOpen] = useState(false);
  return (
    <details onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>Data connections</summary>
      {open ? <ConnectionsBody app={app} env={env} /> : null}
    </details>
  );
}

function ConnectionsBody({ app, env }: Props) {
  const { api, queries, queryClient, isAdmin } = useRouteContext({ from: '/_authed/apps/$appId' });
  const me = queries.useQuery('get', '/v1/whoami');
  const init = { params: { path: { app_id: app.id, environment_id: env.id } } };
  const linked = queries.useQuery('get', PATH, init);
  const [notice, setNotice] = useState<string | null>(null);
  const admin = isAdmin(me.data);
  const target = { appId: app.id, environmentId: env.id };
  const where = ENV_TITLE[env.name];

  function saved(update: EnvironmentConnections, message: string) {
    queryClient.setQueryData(queries.queryOptions('get', PATH, init).queryKey, update);
    setNotice(message);
  }

  const columns: Column<EnvironmentConnection>[] = [
    { header: 'Connection', cell: (l) => <code>{l.connection.name}</code> },
    { header: 'Classification', cell: (l) => l.connection.classification },
    {
      header: 'State',
      cell: (l) => (
        <>
          {l.connection.status === 'active' ? null : <Badge tone="danger">{l.connection.status}</Badge>}{' '}
          {l.connection.setup_status === 'ready' ? (
            <Badge tone="success">ready</Badge>
          ) : (
            <Badge tone="warning">pending setup</Badge>
          )}{' '}
          {l.over_ceiling_since ? (
            <Badge tone="warning">
              shared wider than allowed since {new Date(l.over_ceiling_since).toLocaleDateString()}
            </Badge>
          ) : null}
        </>
      ),
    },
    { header: 'Limits here', cell: (l) => <Phrases items={limitPhrases(l.limits)} /> },
    { header: 'Columns', cell: (l) => <ConnectionColumns app={app} env={env} name={l.connection.name} /> },
    {
      header: 'Linked',
      cell: (l) => <time dateTime={l.granted_at}>{new Date(l.granted_at).toLocaleString()}</time>,
    },
  ];
  if (admin) {
    columns.push({
      header: 'Action',
      className: 'actions-cell',
      cell: (l) => (
        <ConfirmAction
          label="Unlink"
          accessibleLabel={`Unlink ${l.connection.name} from ${where}`}
          title={`Unlink ${l.connection.name}`}
          confirmText={app.slug}
          confirmLabel="Unlink"
          onConfirm={async () =>
            saved(
              await detachConnection(api, target, l.connection.name),
              `Unlinked ${l.connection.name}.`,
            )
          }
        >
          <p>
            {where} of <strong>{app.slug}</strong> stops reaching{' '}
            <code>{l.connection.name}</code> at the gateway&apos;s next snapshot.
          </p>
        </ConfirmAction>
      ),
    });
  }

  return (
    <div className="stack">
      {notice ? (
        <p className="notice notice-success" role="status">
          {notice}
        </p>
      ) : null}
      {linked.isPending ? (
        <p className="muted">Loading connections…</p>
      ) : linked.isError ? (
        <ProblemNotice error={linked.error} />
      ) : (
        <>
          <Table
            caption={`Data connections of ${where}`}
            columns={columns}
            rows={linked.data.connections}
            rowKey={(l) => l.connection.id}
            empty="This environment reaches no data connection."
            emptyHint={
              admin
                ? 'Link one below when the app needs one of your databases.'
                : 'An org admin links a connection here when the app needs one of your databases.'
            }
          />
          {me.isPending ? null : admin ? (
            <LinkForm
              app={app}
              env={env}
              held={linked.data.connections.map((l) => l.connection.name)}
              onSaved={saved}
            />
          ) : (
            <p className="muted">Only org admins can link and unlink connections.</p>
          )}
        </>
      )}
    </div>
  );
}

interface ColumnsProps extends Props {
  readonly name: string;
}

/**
 * The tables and columns this environment sees through one connection, asked of the cell's data
 * gateway only once opened (GA-5.8). The API keeps an answer five minutes.
 */
function ConnectionColumns({ app, env, name }: ColumnsProps) {
  const [open, setOpen] = useState(false);
  return (
    <details onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary aria-label={`Columns of ${name}`}>Columns</summary>
      {open ? <ColumnsBody app={app} env={env} name={name} /> : null}
    </details>
  );
}

function ColumnsBody({ app, env, name }: ColumnsProps) {
  const { queries } = useRouteContext({ from: '/_authed/apps/$appId' });
  const schema = queries.useQuery('get', SCHEMA_PATH, {
    params: { path: { app_id: app.id, environment_id: env.id, name } },
  });
  if (schema.isPending) return <p className="muted">Asking the data gateway…</p>;
  if (schema.isError) return <ProblemNotice error={schema.error} />;
  return <SchemaTables schema={schema.data} />;
}

function SchemaTables({ schema }: { readonly schema: ConnectionSchema }) {
  if (schema.tables.length === 0) return <p className="muted">This connection shows no table here.</p>;
  return (
    <div className="stack">
      {schema.tables.map((t) => (
        <section key={t.name} aria-label={t.name}>
          <strong>
            <code>{t.name}</code>
          </strong>
          <ul>
            {t.columns.map((c) => (
              <li key={c.name}>
                <code>{c.name}</code> {c.type}
                {c.db_type ? <span className="muted"> ({c.db_type})</span> : null}
              </li>
            ))}
          </ul>
        </section>
      ))}
      <p className="muted">
        Snapshot {schema.snapshot_version}
        {schema.cached ? ', as read in the last five minutes' : ''}.
      </p>
    </div>
  );
}

interface LinkProps extends Props {
  readonly held: readonly string[];
  readonly onSaved: (update: EnvironmentConnections, message: string) => void;
}

/**
 * Links a connection. When the environment is shared wider than the connection allows, the API
 * answers APPROVAL_REQUIRED, and the person can ask for an `exceed_ceiling` approval here.
 */
function LinkForm({ app, env, held, onSaved }: LinkProps) {
  const { api, queries } = useRouteContext({ from: '/_authed/apps/$appId' });
  const all = queries.useQuery('get', '/v1/connections');
  const [choice, setChoice] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [needsApproval, setNeedsApproval] = useState<string | null>(null);
  const [asked, setAsked] = useState<Approval | null>(null);
  const selectId = useId();
  const target = { appId: app.id, environmentId: env.id };
  const free = (all.data?.connections ?? []).filter((c) => !held.includes(c.name));

  function pick(name: string) {
    setChoice(name);
    setError(null);
    setNeedsApproval(null);
    setAsked(null);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!choice || busy) return;
    setBusy(true);
    setError(null);
    setNeedsApproval(null);
    setAsked(null);
    try {
      onSaved(await attachConnection(api, target, choice), `Linked ${choice}.`);
      setChoice('');
    } catch (e) {
      if (e instanceof ApiProblem && e.code === 'APPROVAL_REQUIRED') setNeedsApproval(choice);
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  async function ask() {
    if (!needsApproval || busy) return;
    setBusy(true);
    try {
      setAsked(await askCeilingApproval(api, target, needsApproval));
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  if (all.isPending) return <p className="muted">Loading connections to link…</p>;
  if (all.isError) return <ProblemNotice error={all.error} />;
  return (
    <form className="stack" onSubmit={submit} aria-label={`Link a connection to ${ENV_TITLE[env.name]}`}>
      <label className="field" htmlFor={selectId}>
        <span>Connection to link</span>
        <select id={selectId} value={choice} onChange={(e) => pick(e.target.value)}>
          <option value="">{free.length === 0 ? 'No other connection' : 'Pick one'}</option>
          {free.map((c) => (
            <option key={c.id} value={c.name}>
              {c.name} ({c.classification}
              {c.setup_status === 'ready' ? '' : ', pending setup'})
            </option>
          ))}
        </select>
      </label>
      {error ? <ProblemNotice error={error} /> : null}
      {needsApproval && !asked ? (
        <div className="notice notice-info" role="note">
          <p>
            This environment is shared wider than <code>{needsApproval}</code> allows. Its owner or
            an org admin must approve that; ask here, then link it again once it is approved.
          </p>
          <div>
            <Button onClick={() => void ask()} disabled={busy}>
              Ask for approval
            </Button>
          </div>
        </div>
      ) : null}
      {asked ? (
        <p className="notice notice-success" role="status">
          Asked for approval:{' '}
          <Link to="/approvals/$approvalId" params={{ approvalId: asked.id }}>
            {asked.id}
          </Link>{' '}
          ({asked.state}). Link it again once it is approved.
        </p>
      ) : null}
      <div>
        <Button type="submit" variant="primary" disabled={!choice || busy}>
          Link connection
        </Button>
      </div>
    </form>
  );
}
