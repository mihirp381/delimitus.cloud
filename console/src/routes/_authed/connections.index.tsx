import { createFileRoute, Link, useNavigate } from '@tanstack/react-router';
import { useId, useState } from 'react';
import { type Connection, KIND_TITLE } from '../../api/connections';
import { AdminOnly } from '../../components/AdminOnly';
import { Loading } from '../../components/Loading';
import { PageHeader } from '../../components/PageHeader';
import { Person } from '../../components/Person';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';
import { AddConnection } from '../../connections/AddConnection';
import { ConnectionState } from '../../connections/State';

export const Route = createFileRoute('/_authed/connections/')({
  component: ConnectionsPage,
});

const SEARCH_FROM = 10;

const COLUMNS: readonly Column<Connection>[] = [
  {
    header: 'Name',
    cell: (c) => (
      <Link to="/connections/$connectionId" params={{ connectionId: c.id }}>
        {c.name}
      </Link>
    ),
  },
  { header: 'Kind', cell: (c) => KIND_TITLE[c.kind] },
  { header: 'State', cell: (c) => <ConnectionState connection={c} /> },
  { header: 'Owner', cell: (c) => <Person name={c.owner_name} email={c.owner_email} fallback="None" /> },
];

function matches(c: Connection, text: string): boolean {
  const t = text.trim().toLowerCase();
  return !t || c.name.includes(t) || c.kind.includes(t) || KIND_TITLE[c.kind].toLowerCase().includes(t);
}

function ConnectionsPage() {
  const { queries, isAdmin } = Route.useRouteContext();
  const me = queries.useQuery('get', '/v1/whoami');
  const list = queries.useQuery('get', '/v1/connections');
  const navigate = useNavigate();
  const [text, setText] = useState('');
  const searchId = useId();
  const admin = isAdmin(me.data);
  const all = list.data?.connections ?? [];
  const rows = all.filter((c) => matches(c, text));

  return (
    <>
      <PageHeader
        title="Connections"
        purpose="Your company's databases that apps may read through the data gateway."
        about={
          <p>
            A connection&apos;s address is stored when it is added and never shown again; its credentials are set up
            with your operator.
          </p>
        }
      >
        <AdminOnly quiet>
          <AddConnection />
        </AdminOnly>
      </PageHeader>
      {me.isPending || admin ? null : (
        <p className="muted">
          Only org admins can add or change connections. You see every connection of the org, never its address.
        </p>
      )}
      {all.length > SEARCH_FROM ? (
        <div className="search">
          <label htmlFor={searchId} className="visually-hidden">
            Filter connections
          </label>
          <input
            id={searchId}
            type="search"
            placeholder="Filter by name or kind"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </div>
      ) : null}
      <section className="panel" aria-label="Connections">
        {list.isPending ? (
          <Loading label="Loading connections…" rows={3} />
        ) : list.isError ? (
          <ProblemNotice error={list.error} />
        ) : (
          <Table
            caption="Connections"
            columns={COLUMNS}
            rows={rows}
            rowKey={(c) => c.id}
            onOpen={(c) => void navigate({ to: '/connections/$connectionId', params: { connectionId: c.id } })}
            empty={all.length > 0 ? `No connection matches ${text.trim()}.` : 'No connections yet.'}
            emptyHint={
              all.length > 0
                ? undefined
                : admin
                  ? 'Add a connection to let apps read one of your databases through the data gateway.'
                  : 'An org admin adds connections. Ask one for the database your app needs.'
            }
          />
        )}
      </section>
    </>
  );
}
