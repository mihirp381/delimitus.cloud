import { createFileRoute, Link } from '@tanstack/react-router';
import { KIND_TITLE } from '../../api/connections';
import { Badge } from '../../components/Badge';
import { Loading } from '../../components/Loading';
import { Notice } from '../../components/Notice';
import { PageHeader } from '../../components/PageHeader';
import { ProblemNotice } from '../../components/ProblemNotice';
import { Overview } from '../../connections/Overview';
import { Limits, Schemas } from '../../connections/Settings';
import { Status } from '../../connections/Status';
import { ConnectionState } from '../../connections/State';

export const Route = createFileRoute('/_authed/connections/$connectionId')({
  component: ConnectionPage,
});

function ConnectionPage() {
  const { connectionId } = Route.useParams();
  const { queries } = Route.useRouteContext();
  const list = queries.useQuery('get', '/v1/connections');
  if (list.isPending) return <Loading label="Loading the connection…" rows={5} />;
  if (list.isError) return <ProblemNotice error={list.error} />;
  const c = list.data.connections.find((x) => x.id === connectionId);
  if (!c) {
    return (
      <>
        <PageHeader title="Connection not found" />
        <Notice tone="warn">
          <p>
            This organisation has no such connection. <Link to="/connections">See every connection</Link>.
          </p>
        </Notice>
      </>
    );
  }
  return (
    <>
      <PageHeader
        title={c.name}
        status={
          <>
            <Badge tone="neutral">{KIND_TITLE[c.kind]}</Badge> <ConnectionState connection={c} />
          </>
        }
        purpose="A source apps read through the data gateway; its address is never shown."
      />
      <Overview connection={c} />
      <Schemas connection={c} />
      <Limits connection={c} />
      <Status connection={c} />
    </>
  );
}
