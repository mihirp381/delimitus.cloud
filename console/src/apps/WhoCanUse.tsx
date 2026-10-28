import { Link, useRouteContext } from '@tanstack/react-router';
import type { InventoryApp, InventoryEnvironment } from '../api/inventory';
import { ENV_TITLE } from '../app-detail/names';
import { who } from '../app-detail/useGrants';
import { Badge } from '../components/Badge';
import { Button } from '../components/Button';
import { Dialog } from '../components/Dialog';
import { Loading } from '../components/Loading';
import { ProblemNotice } from '../components/ProblemNotice';

const ORDER = ['prod', 'preview'] as const;

function EnvGrants({ app, env }: { readonly app: InventoryApp; readonly env: InventoryEnvironment }) {
  const { queries } = useRouteContext({ from: '/_authed' });
  const grants = queries.useQuery('get', '/v1/apps/{app_id}/environments/{environment_id}/grants', {
    params: { path: { app_id: app.app_id, environment_id: env.environment_id } },
  });
  const title = ENV_TITLE[env.name];
  return (
    <section className="who-env" aria-label={title}>
      <h3>{title}</h3>
      {grants.isPending ? (
        <Loading label={`Loading who can use ${title.toLowerCase()}…`} rows={2} />
      ) : grants.isError ? (
        <ProblemNotice error={grants.error} />
      ) : grants.data.grants.length ? (
        <ul className="who-list">
          {grants.data.grants.map((g) => (
            <li key={g.id}>
              <span>{who(g)}</span>
              <Badge tone={g.role === 'builder' ? 'info' : 'neutral'}>{g.role}</Badge>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">Nobody yet.</p>
      )}
    </section>
  );
}

interface Props {
  readonly app: InventoryApp | null;
  readonly onClose: () => void;
}

/** Who may use each environment of one app, read from its sharing rules. */
export function WhoCanUse({ app, onClose }: Props) {
  return (
    <Dialog open={app !== null} title={app ? `Who can use ${app.slug}` : 'Who can use'} onClose={onClose}>
      {app ? (
        <>
          {ORDER.flatMap((name) => app.environments.filter((e) => e.name === name)).map((env) => (
            <EnvGrants key={env.environment_id} app={app} env={env} />
          ))}
          <div className="actions who-actions">
            <Link to="/apps/$appId" params={{ appId: app.app_id }} search={{ tab: 'access' }}>
              Change on the Access tab
            </Link>
            <Button variant="primary" onClick={onClose}>
              Close
            </Button>
          </div>
        </>
      ) : null}
    </Dialog>
  );
}
