import { createFileRoute } from '@tanstack/react-router';
import { DeleteProgress } from '../../app-detail/DeleteRun';
import { PageHeader } from '../../components/PageHeader';

export const Route = createFileRoute('/_authed/apps/$appId_/delete/$runId')({
  component: DeleteRunPage,
});

/**
 * One removal of an app on its own page: what to open, or send, to see what was deleted and
 * where a removal stopped. It can still be read once the app is gone.
 */
function DeleteRunPage() {
  const { appId, runId } = Route.useParams();
  return (
    <>
      <PageHeader
        title="Delete run"
        purpose="One removal of an app: what it deleted, step by step, and how long each step took."
        about={<p>Every step is also in the audit log.</p>}
      />
      <section className="panel" aria-label="Delete run">
        <DeleteProgress appId={appId} runId={runId} page />
      </section>
    </>
  );
}
