import { useInfiniteQuery } from '@tanstack/react-query';
import { createFileRoute, Link, useNavigate } from '@tanstack/react-router';
import { type FormEvent, useId, useState } from 'react';
import {
  ACTOR_KINDS,
  AUDIT_ACTIONS,
  type AuditEvent,
  type AuditFilters,
  auditFilters,
  type ExportFormat,
  exportAudit,
} from '../../api/audit';
import { must } from '../../api/client';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';
import { saveBlob } from '../../download';

export const Route = createFileRoute('/_authed/audit')({
  validateSearch: auditFilters,
  component: AuditPage,
});

const FORMATS: readonly { readonly format: ExportFormat; readonly label: string }[] = [
  { format: 'csv', label: 'Export CSV' },
  { format: 'jsonl', label: 'Export JSON Lines' },
];

function AuditPage() {
  const { queries, isAdmin } = Route.useRouteContext();
  const me = queries.useQuery('get', '/v1/whoami');
  if (isAdmin(me.data)) {
    return <AuditLog />;
  }
  return (
    <>
      <h1>Audit log</h1>
      {me.isPending ? (
        <p className="muted">Loading…</p>
      ) : me.isError ? (
        <ProblemNotice error={me.error} />
      ) : (
        <p className="muted">Only org admins can see the audit log.</p>
      )}
    </>
  );
}

function AuditLog() {
  const { api, queryClient } = Route.useRouteContext();
  // The router passes unvalidated search keys through; the API refuses unknown parameters.
  const filters = auditFilters(Route.useSearch());
  const navigate = useNavigate({ from: Route.fullPath });
  const events = useInfiniteQuery({
    queryKey: ['audit', filters],
    queryFn: async ({ pageParam, signal }) =>
      must(
        await api.GET('/v1/audit', {
          params: { query: pageParam === null ? filters : { ...filters, before_seq: pageParam } },
          signal,
        }),
      ),
    initialPageParam: null as number | null,
    getNextPageParam: (page) => page.next_before_seq,
  });
  const [exporting, setExporting] = useState<ExportFormat | null>(null);
  const [exportError, setExportError] = useState<unknown>(null);
  const [saved, setSaved] = useState<string | null>(null);

  async function download(format: ExportFormat) {
    setExporting(format);
    setExportError(null);
    setSaved(null);
    try {
      const { blob, fileName } = await exportAudit(api, filters, format);
      saveBlob(blob, fileName);
      setSaved(fileName);
      await queryClient.invalidateQueries({ queryKey: ['audit'] });
    } catch (error) {
      setExportError(error);
    } finally {
      setExporting(null);
    }
  }

  const rows = events.data?.pages.flatMap((p) => p.events) ?? [];

  return (
    <>
      <div className="page-head">
        <h1>Audit log</h1>
        <div className="actions">
          {FORMATS.map(({ format, label }) => (
            <Button key={format} disabled={exporting !== null} onClick={() => void download(format)}>
              {exporting === format ? 'Exporting…' : label}
            </Button>
          ))}
        </div>
      </div>
      <p className="muted">
        Newest first. An export holds every event matching the applied filters, oldest first, and is
        itself recorded as <code>audit.exported</code>.
      </p>
      {saved ? (
        <p className="notice notice-success" role="status">
          Saved {saved}.
        </p>
      ) : null}
      {exportError ? <ProblemNotice error={exportError} /> : null}
      <FilterForm
        key={JSON.stringify(filters)}
        filters={filters}
        onApply={(next) => void navigate({ search: next })}
      />
      {events.data ? (
        <section className="panel">
          <Table
            caption="Audit events"
            columns={COLUMNS}
            rows={rows}
            rowKey={(e) => String(e.seq)}
            empty={Object.keys(filters).length ? 'No event matches the filters.' : 'No events yet.'}
          />
          <div className="more">
            <p className="muted count" aria-live="polite">
              {rows.length} {rows.length === 1 ? 'event' : 'events'}
              {events.hasNextPage ? ', more are older' : ''}
            </p>
            {events.hasNextPage ? (
              <Button disabled={events.isFetchingNextPage} onClick={() => void events.fetchNextPage()}>
                {events.isFetchingNextPage ? 'Loading…' : 'Load older events'}
              </Button>
            ) : null}
          </div>
          {events.isError ? <ProblemNotice error={events.error} /> : null}
        </section>
      ) : events.isError ? (
        <ProblemNotice error={events.error} />
      ) : (
        <p className="muted">Loading events…</p>
      )}
    </>
  );
}

/** A `datetime-local` value (wall-clock time here) for an instant. */
function localInput(iso: string | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, '0');
  const day = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  return `${day}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function FilterForm({
  filters,
  onApply,
}: {
  readonly filters: AuditFilters;
  readonly onApply: (next: AuditFilters) => void;
}) {
  const id = useId();

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // `datetime-local` gives a time without an offset, which Date reads as local time.
    onApply(auditFilters(Object.fromEntries(new FormData(event.currentTarget))));
  }

  const field = (name: string) => `${id}-${name}`;
  return (
    <form className="panel filters" aria-label="Filter events" onSubmit={submit}>
      <label className="field" htmlFor={field('action')}>
        <span>Action</span>
        <select id={field('action')} name="action" defaultValue={filters.action ?? ''}>
          <option value="">Any action</option>
          {AUDIT_ACTIONS.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
      </label>
      <label className="field" htmlFor={field('actor_kind')}>
        <span>Actor kind</span>
        <select id={field('actor_kind')} name="actor_kind" defaultValue={filters.actor_kind ?? ''}>
          <option value="">Any kind</option>
          {ACTOR_KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </label>
      <label className="field" htmlFor={field('actor_id')}>
        <span>Actor id</span>
        <input
          id={field('actor_id')}
          name="actor_id"
          maxLength={200}
          defaultValue={filters.actor_id ?? ''}
        />
      </label>
      <label className="field" htmlFor={field('target_kind')}>
        <span>Target kind</span>
        <input
          id={field('target_kind')}
          name="target_kind"
          maxLength={200}
          placeholder="for example app"
          defaultValue={filters.target_kind ?? ''}
        />
      </label>
      <label className="field" htmlFor={field('target_id')}>
        <span>Target id</span>
        <input
          id={field('target_id')}
          name="target_id"
          maxLength={200}
          defaultValue={filters.target_id ?? ''}
        />
      </label>
      <label className="field" htmlFor={field('since')}>
        <span>From (inclusive)</span>
        <input
          id={field('since')}
          name="since"
          type="datetime-local"
          defaultValue={localInput(filters.since)}
        />
      </label>
      <label className="field" htmlFor={field('until')}>
        <span>Until (exclusive)</span>
        <input
          id={field('until')}
          name="until"
          type="datetime-local"
          defaultValue={localInput(filters.until)}
        />
      </label>
      <div className="actions">
        <Button type="submit" variant="primary">
          Search
        </Button>
        <Button onClick={() => onApply({})}>Clear filters</Button>
      </div>
    </form>
  );
}

function Changes({ event }: { readonly event: AuditEvent }) {
  if (event.before === null && event.after === null && event.policy_decision_id === null) return null;
  return (
    <details>
      <summary>Details</summary>
      <dl className="facts">
        {event.before !== null ? (
          <>
            <dt>Before</dt>
            <dd>
              <pre>{JSON.stringify(event.before, null, 2)}</pre>
            </dd>
          </>
        ) : null}
        {event.after !== null ? (
          <>
            <dt>After</dt>
            <dd>
              <pre>{JSON.stringify(event.after, null, 2)}</pre>
            </dd>
          </>
        ) : null}
        {event.policy_decision_id !== null ? (
          <>
            <dt>Policy decision</dt>
            <dd>
              <code>{event.policy_decision_id}</code>
            </dd>
          </>
        ) : null}
      </dl>
    </details>
  );
}

const COLUMNS: readonly Column<AuditEvent>[] = [
  {
    header: 'When',
    cell: (e) => <time dateTime={e.at}>{new Date(e.at).toLocaleString()}</time>,
  },
  { header: 'Action', cell: (e) => <code>{e.action}</code> },
  {
    header: 'Actor',
    cell: (e) => (
      <>
        {e.actor.kind} <code>{e.actor.id}</code>
        {e.actor.via_agent ? (
          <>
            {' '}
            <Badge tone="info">agent</Badge>
            {e.actor.client_id ? (
              <>
                {' '}
                <code>{e.actor.client_id}</code>
              </>
            ) : null}
          </>
        ) : null}
      </>
    ),
  },
  {
    header: 'Target',
    cell: (e) => (
      <>
        {e.target.kind}{' '}
        {e.target.kind === 'app' ? (
          <Link to="/apps/$appId" params={{ appId: e.target.id }}>
            <code>{e.target.id}</code>
          </Link>
        ) : (
          <code>{e.target.id}</code>
        )}
      </>
    ),
  },
  { header: 'Details', cell: (e) => <Changes event={e} /> },
  { header: 'Seq', cell: (e) => e.seq },
];
