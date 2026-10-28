import { Link, useRouteContext } from '@tanstack/react-router';
import { countText } from '../api/inventory';
import { accessText, isBuilder, matchesOpenFilter, type UserApp } from '../api/openApps';
import { ENV_TITLE } from '../app-detail/names';
import { Badge, STATUS_TONE } from '../components/Badge';
import { Icon } from '../components/Icon';
import { Loading } from '../components/Loading';
import { ProblemNotice } from '../components/ProblemNotice';
import { type Column, Table } from '../components/Table';

export const DIRECT = 'Shared with you';

function Slug({ app }: { readonly app: UserApp }) {
  return (
    <span className="app-name">
      <span className={`dot dot-${STATUS_TONE[app.status] ?? 'warning'}`} role="img" aria-label={app.status} title={app.status} />
      {isBuilder(app) ? (
        <Link to="/apps/$appId" params={{ appId: app.app_id }}>
          {app.slug}
        </Link>
      ) : (
        <span>{app.slug}</span>
      )}
    </span>
  );
}

function Access({ app }: { readonly app: UserApp }) {
  return (
    <span className="open-access">
      {accessText(app, DIRECT)}
      {isBuilder(app) ? <Badge tone="info">Builder</Badge> : null}
    </span>
  );
}

function Open({ app }: { readonly app: UserApp }) {
  return (
    <span className="open-buttons">
      {app.environments.map((e) =>
        e.url ? (
          <a
            key={e.environment_id}
            className={e.name === 'prod' ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
            href={e.url}
            target="_blank"
            rel="noopener"
          >
            Open {ENV_TITLE[e.name].toLowerCase()}
            <span className="visually-hidden"> {app.slug}</span>
            <Icon name="external" />
          </a>
        ) : null,
      )}
    </span>
  );
}

const COLUMNS: readonly Column<UserApp>[] = [
  { header: 'App', cell: (app) => <Slug app={app} /> },
  { header: 'Access', className: 'fill', cell: (app) => <Access app={app} /> },
  { header: 'Open', className: 'nowrap', cell: (app) => <Open app={app} /> },
];

interface Props {
  readonly text: string;
  readonly empty: string;
  readonly emptyHint?: string;
}

/** The apps the caller may open, from the one access evaluator: one row per app. */
export function OpenApps({ text, empty, emptyHint }: Props) {
  const { queries } = useRouteContext({ from: '/_authed' });
  const mine = queries.useQuery('get', '/v1/me/apps');
  if (mine.isPending) return <Loading label="Loading apps…" />;
  if (mine.isError) return <ProblemNotice error={mine.error} />;
  const all = mine.data.apps;
  const shown = all.filter((app) => matchesOpenFilter(app, text, DIRECT));
  return (
    <section className="panel open-apps">
      <Table
        caption="Apps you can open"
        columns={COLUMNS}
        rows={shown}
        rowKey={(app) => app.app_id}
        empty={all.length ? 'No app matches the filter.' : empty}
        emptyHint={all.length ? 'Clear the filter to see every app.' : emptyHint}
      />
      <p className="muted count" aria-live="polite">
        {countText(shown.length, all.length)}
      </p>
    </section>
  );
}
