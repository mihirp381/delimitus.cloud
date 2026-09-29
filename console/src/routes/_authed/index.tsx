import { createFileRoute, Link } from '@tanstack/react-router';
import { useId, useState } from 'react';
import type { components } from '../../api/schema';
import { StatusBadge } from '../../components/Badge';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';

type AppSummary = components['schemas']['AppSummary'];

export const Route = createFileRoute('/_authed/')({
  component: Inventory,
});

const COLUMNS: readonly Column<AppSummary>[] = [
  {
    header: 'App',
    cell: (app) => (
      <Link to="/apps/$appId" params={{ appId: app.id }}>
        {app.slug}
      </Link>
    ),
  },
  { header: 'Status', cell: (app) => <StatusBadge status={app.status} /> },
  { header: 'Owner', cell: (app) => <code>{app.owner_user_id}</code> },
  { header: 'App id', cell: (app) => <code>{app.id}</code> },
];

function matches(app: AppSummary, filter: string): boolean {
  const f = filter.trim().toLowerCase();
  return !f || app.slug.includes(f) || app.owner_user_id.toLowerCase().includes(f);
}

function Inventory() {
  const { queries } = Route.useRouteContext();
  const apps = queries.useQuery('get', '/v1/apps');
  const [filter, setFilter] = useState('');
  const filterId = useId();

  const all = apps.data ? [...apps.data.apps].sort((a, b) => a.slug.localeCompare(b.slug)) : [];
  const shown = all.filter((app) => matches(app, filter));

  return (
    <>
      <div className="page-head">
        <h1>Apps</h1>
        <div className="search">
          <label htmlFor={filterId} className="visually-hidden">
            Filter apps
          </label>
          <input
            id={filterId}
            type="search"
            placeholder="Filter by slug or owner"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>
      </div>
      {apps.isPending ? (
        <p className="muted">Loading apps…</p>
      ) : apps.isError ? (
        <ProblemNotice error={apps.error} />
      ) : (
        <section className="panel">
          <Table
            caption="Apps in this organisation"
            columns={COLUMNS}
            rows={shown}
            rowKey={(app) => app.id}
            empty={all.length ? 'No app matches the filter.' : 'No apps yet. An app appears here once it is created.'}
          />
          <p className="muted count" aria-live="polite">
            {shown.length === all.length
              ? `${all.length} ${all.length === 1 ? 'app' : 'apps'}`
              : `${shown.length} of ${all.length} apps`}
          </p>
        </section>
      )}
    </>
  );
}
