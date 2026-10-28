import { useRouteContext } from '@tanstack/react-router';
import { useId } from 'react';
import { type UserApp, type UserAppEnvironment, viaText } from '../api/openApps';
import type { UserMatch } from '../api/people';
import { ENV_TITLE } from '../app-detail/names';
import { AdminOnly } from '../components/AdminOnly';
import { Badge } from '../components/Badge';
import { Loading } from '../components/Loading';
import { ProblemNotice } from '../components/ProblemNotice';
import { type Column, Table } from '../components/Table';

const DIRECT = 'Shared with this person';

interface Row {
  readonly app: UserApp;
  readonly env: UserAppEnvironment;
}

const COLUMNS: readonly Column<Row>[] = [
  { header: 'App', cell: (r) => r.app.slug },
  { header: 'Environment', cell: (r) => ENV_TITLE[r.env.name] },
  { header: 'Role', cell: (r) => <Badge tone={r.env.role === 'builder' ? 'info' : 'neutral'}>{r.env.role}</Badge> },
  { header: 'Via', cell: (r) => r.env.via.map((v) => viaText(v, DIRECT)).join(', ') },
];

function Listed({ userId }: { readonly userId: string }) {
  const { queries } = useRouteContext({ from: '/_authed' });
  const apps = queries.useQuery('get', '/v1/users/{user_id}/apps', { params: { path: { user_id: userId } } });
  if (apps.isPending) return <Loading label="Loading apps…" rows={2} />;
  if (apps.isError) return <ProblemNotice error={apps.error} />;
  const rows = apps.data.apps.flatMap((app) => app.environments.map((env) => ({ app, env })));
  return (
    <Table
      caption="Apps this person can open"
      columns={COLUMNS}
      rows={rows}
      rowKey={(r) => r.env.environment_id}
      empty="Nothing is shared with this person yet."
      dense
    />
  );
}

/** What the sharing rules let one person open, as the access evaluator decides it. */
export function PersonApps({ person }: { readonly person: UserMatch }) {
  const headingId = useId();
  return (
    <AdminOnly quiet>
      <section className="dialog-section" aria-labelledby={headingId}>
        <h3 id={headingId}>Apps this person can open</h3>
        {person.status === 'active' ? (
          <Listed userId={person.id} />
        ) : (
          <p className="muted">A person who is not active cannot open any app.</p>
        )}
      </section>
    </AdminOnly>
  );
}
