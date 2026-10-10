import { useQuery, useQueryClient } from '@tanstack/react-query';
import { createFileRoute, Link } from '@tanstack/react-router';
import { type FormEvent, useId, useState } from 'react';
import {
  type ApprovalDecided,
  type ApprovalDetail,
  appliedText,
  cancelApproval,
  connectionName,
  decideApproval,
  diffGrantText,
  type Outcome,
  REASON_MAX,
} from '../../api/approvals';
import { must } from '../../api/client';
import { ApprovalBadge, Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { PageHeader } from '../../components/PageHeader';
import { ProblemNotice } from '../../components/ProblemNotice';

export const Route = createFileRoute('/_authed/approvals/$approvalId')({
  component: ApprovalPage,
});

function ApprovalPage() {
  const { approvalId } = Route.useParams();
  const { api } = Route.useRouteContext();
  const detail = useQuery({
    queryKey: ['approval', approvalId],
    queryFn: async ({ signal }) =>
      must(
        await api.GET('/v1/approvals/{approval_id}', {
          params: { path: { approval_id: approvalId } },
          signal,
        }),
      ),
  });
  return (
    <>
      <p className="crumbs">
        <Link to="/approvals">Back to approvals</Link>
      </p>
      {detail.data ? (
        <Detail approval={detail.data} />
      ) : detail.isError ? (
        <ProblemNotice error={detail.error} />
      ) : (
        <p className="muted">Loading the request…</p>
      )}
    </>
  );
}

function Detail({ approval: a }: { readonly approval: ApprovalDetail }) {
  const { api } = Route.useRouteContext();
  const queryClient = useQueryClient();
  const reasonId = useId();
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<string | null>(null);
  const trimmed = reason.trim();
  const valid = trimmed.length >= 1 && trimmed.length <= REASON_MAX;

  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: ['approval', a.id] });
    await queryClient.invalidateQueries({ queryKey: ['approvals'] });
  }

  async function act(work: () => Promise<string>) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await work());
      await refresh();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  function decide(outcome: Outcome) {
    if (!valid) return;
    void act(async () => {
      const done: ApprovalDecided = await decideApproval(api, a.id, outcome, trimmed);
      return outcome === 'approved' ? appliedText(done) : 'Rejected. The requester has been told why.';
    });
  }

  function submit(event: FormEvent) {
    event.preventDefault();
  }

  return (
    <>
      <PageHeader title="Approval request" status={<ApprovalBadge state={a.state} />} />
      <section className="panel tone-violet">
        <dl className="facts">
          <dt>App</dt>
          <dd>
            <Link to="/apps/$appId" params={{ appId: a.app_id }}>
              {a.app}
            </Link>{' '}
            {a.environment}
          </dd>
          <dt>Asked by</dt>
          <dd>
            {a.requested_by_name}
            {a.requested_via_agent ? (
              <>
                {' '}
                <Badge tone="info">agent</Badge>
              </>
            ) : null}{' '}
            on <time dateTime={a.created_at}>{new Date(a.created_at).toLocaleString()}</time>
          </dd>
        </dl>
        <Change approval={a} />
        <Outcome approval={a} />
      </section>
      {result ? (
        <div className="notice notice-info" role="status">
          <p>{result}</p>
        </div>
      ) : null}
      {a.can_decide ? (
        <form className="panel tone-violet" onSubmit={submit} aria-label="Decide this request">
          <label htmlFor={reasonId}>Reason</label>
          <textarea
            id={reasonId}
            value={reason}
            maxLength={REASON_MAX}
            rows={3}
            onChange={(e) => setReason(e.target.value)}
          />
          <p className="muted">
            Required, up to {REASON_MAX} characters. The requester is sent it with the decision.
          </p>
          <div className="actions">
            <Button variant="primary" disabled={!valid || busy} onClick={() => decide('approved')}>
              Approve
            </Button>
            <Button variant="danger" disabled={!valid || busy} onClick={() => decide('denied')}>
              Reject
            </Button>
          </div>
        </form>
      ) : null}
      {a.can_cancel ? (
        <div className="panel">
          <Button
            disabled={busy}
            onClick={() =>
              void act(async () => {
                await cancelApproval(api, a.id);
                return 'The request was withdrawn.';
              })
            }
          >
            Withdraw this request
          </Button>
        </div>
      ) : null}
      {error ? <ProblemNotice error={error} /> : null}
    </>
  );
}

function Change({ approval: a }: { readonly approval: ApprovalDetail }) {
  if (a.grant_diff) {
    const { added, removed } = a.grant_diff;
    return (
      <>
        <h2>Change to who has access</h2>
        {added.length === 0 && removed.length === 0 ? (
          <p className="muted">The sharing already matches what was asked for.</p>
        ) : null}
        {added.length > 0 ? (
          <>
            <h3>Added</h3>
            <ul className="plain-list">
              {added.map((g) => (
                <li key={`${g.role}:${g.subject_kind}:${g.subject_id ?? ''}`}>{diffGrantText(g)}</li>
              ))}
            </ul>
          </>
        ) : null}
        {removed.length > 0 ? (
          <>
            <h3>Removed</h3>
            <ul className="plain-list">
              {removed.map((g) => (
                <li key={`${g.role}:${g.subject_kind}:${g.subject_id ?? ''}`}>{diffGrantText(g)}</li>
              ))}
            </ul>
          </>
        ) : null}
        {a.connection ? (
          <p>
            Goes beyond the audience ceiling of the connection <code>{a.connection.name}</code> (
            {a.connection.classification}, ceiling {a.connection.ceiling_audience}
            {a.connection.ceiling_audience === 'subjects'
              ? `, ${a.connection.ceiling_subjects} named`
              : ''}
            ).
          </p>
        ) : null}
      </>
    );
  }
  if (a.kind === 'connect_data_source') {
    return (
      <p>
        Connect the data source <code>{a.subject_key}</code>. Approving records the decision; it
        changes nothing by itself.
      </p>
    );
  }
  if (a.kind === 'enable_internet_hosts') {
    return (
      <p>
        Allow the internet host <code>{a.subject_key}</code>. Approving adds{' '}
        <code>{a.subject_key}</code> to your org&apos;s allowlist.
      </p>
    );
  }
  return (
    <p>
      <code>{connectionName(a)}</code>
    </p>
  );
}

function Outcome({ approval: a }: { readonly approval: ApprovalDetail }) {
  if (a.state === 'pending') return <p className="muted">Waiting for an approver.</p>;
  if (a.state === 'cancelled') return <p className="muted">Withdrawn by the requester.</p>;
  return (
    <>
      <h2>Decision</h2>
      <p>
        {a.state === 'approved' ? 'Approved' : 'Rejected'} by{' '}
        {a.decided_by_user_id ? <code>{a.decided_by_user_id}</code> : 'an unnamed approver'}
        {a.decision_channel ? ` by ${a.decision_channel}` : ''}
        {a.decided_at ? (
          <>
            {' '}
            on <time dateTime={a.decided_at}>{new Date(a.decided_at).toLocaleString()}</time>
          </>
        ) : null}
      </p>
      {a.decision_reason ? <p>“{a.decision_reason}”</p> : null}
      {a.recorded_by_operator ? (
        <p className="muted">
          Recorded by SSC staff <code>{a.recorded_by_operator}</code>
        </p>
      ) : null}
    </>
  );
}
