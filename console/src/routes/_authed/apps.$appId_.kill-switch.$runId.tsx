import { createFileRoute } from '@tanstack/react-router';
import { KillSwitchProgress } from '../../app-detail/KillSwitchRun';
import { PageHeader } from '../../components/PageHeader';

export const Route = createFileRoute('/_authed/apps/$appId_/kill-switch/$runId')({
  component: KillSwitchRunPage,
});

/**
 * One kill switch run on its own page: what to open, or send, to see how long the stop took. The
 * way back to the app is the page bar's breadcrumb.
 */
function KillSwitchRunPage() {
  const { appId, runId } = Route.useParams();
  return (
    <>
      <PageHeader
        title="Kill switch run"
        purpose="One pull of the kill switch: what it did, step by step, and how long each step and the whole stop took. Every step is also in the audit log."
      />
      <section className="panel" aria-label="Kill switch run">
        <KillSwitchProgress appId={appId} runId={runId} page />
      </section>
    </>
  );
}
