import { useInfiniteQuery } from '@tanstack/react-query';
import { createFileRoute, Link, useNavigate } from '@tanstack/react-router';
import { type ChangeEvent, type FormEvent, useId, useState } from 'react';
import {
  ACTOR_KINDS,
  AUDIT_ACTIONS,
  type AuditEvent,
  type AuditFilters,
  auditFilters,
  type ExportFormat,
  exportAudit,
  killSwitchApp,
} from '../../api/audit';
import { must } from '../../api/client';
import { type ChainReport, chainVerdict, checkChain } from '../../auditChain';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { PageHeader } from '../../components/PageHeader';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';
import { saveBlob } from '../../download';

export const Route = createFileRoute('/_authed/audit')({
  validateSearch: auditFilters,
  component: AuditPage,
});

/** Filters that leave events out of the middle of an export, so its seqs skip. */
const ROW_FILTERS = ['action', 'actor_kind', 'actor_id', 'target_kind', 'target_id'] as const;

/** One file's chain check: running, done, or failed to run. */
type Check =
  | { readonly fileName: string; readonly state: 'checking' }
  | { readonly fileName: string; readonly state: 'done'; readonly report: ChainReport; readonly rowFilters: boolean }
  | { readonly fileName: string; readonly state: 'failed'; readonly error: unknown };

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
      <PageHeader title="Audit log" />
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
  const [check, setCheck] = useState<Check | null>(null);

  /** Checks the hash chain of `file`'s bytes here in the browser; the file itself is only read. */
  async function verify(file: Blob, fileName: string, rowFilters: boolean) {
    setCheck({ fileName, state: 'checking' });
    try {
      const report = await checkChain(new Uint8Array(await file.arrayBuffer()));
      setCheck({ fileName, state: 'done', report, rowFilters });
    } catch (error) {
      setCheck({ fileName, state: 'failed', error });
    }
  }

  async function download(format: ExportFormat) {
    setExporting(format);
    setExportError(null);
    setSaved(null);
    setCheck(null);
    try {
      const { blob, fileName } = await exportAudit(api, filters, format);
      // The browser saves the API's bytes as they came; the check below reads the same bytes.
      saveBlob(blob, fileName);
      setSaved(fileName);
      if (format === 'jsonl') {
        await verify(blob, fileName, ROW_FILTERS.some((name) => filters[name] !== undefined));
      }
      await queryClient.invalidateQueries({ queryKey: ['audit'] });
    } catch (error) {
      setExportError(error);
    } finally {
      setExporting(null);
    }
  }

  function verifyFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    // Emptied so that choosing the same file again checks it again.
    event.currentTarget.value = '';
    if (!file) return;
    setSaved(null);
    setExportError(null);
    void verify(file, file.name, false);
  }

  const rows = events.data?.pages.flatMap((p) => p.events) ?? [];
  const hint = useId();

  return (
    <>
      <PageHeader
        title="Audit log"
        purpose={
          <>
            Newest first. An export holds every event matching the applied filters, oldest first,
            and is itself recorded as <code>audit.exported</code>. A JSON Lines export carries the
            log's hash chain, which is checked here in your browser once it is saved.
          </>
        }
      >
        {FORMATS.map(({ format, label }) => (
          <Button key={format} disabled={exporting !== null} onClick={() => void download(format)}>
            {exporting === format ? 'Exporting…' : label}
          </Button>
        ))}
        <label className="btn btn-secondary file-btn">
          Verify a file
          <input
            type="file"
            className="visually-hidden"
            accept=".jsonl,.ndjson,application/x-ndjson"
            aria-describedby={hint}
            onChange={verifyFile}
          />
        </label>
        <span id={hint} className="visually-hidden">
          Checks the hash chain of a JSON Lines export you saved earlier. The file stays on this
          computer.
        </span>
      </PageHeader>
      {saved ? (
        <p className="notice notice-success" role="status">
          Saved {saved}.
        </p>
      ) : null}
      {exportError ? <ProblemNotice error={exportError} /> : null}
      {check ? <ChainNotice check={check} /> : null}
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
            emptyHint={
              Object.keys(filters).length
                ? 'Widen or clear the filters to see more.'
                : 'Every change made in your organisation is recorded here as it happens.'
            }
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

/** What the chain check found in one file, in the words `ssc audit verify` uses. */
function ChainNotice({ check }: { readonly check: Check }) {
  if (check.state === 'checking') {
    return (
      <p className="notice notice-info" role="status" aria-label="Hash chain check">
        Checking the hash chain of {check.fileName}…
      </p>
    );
  }
  if (check.state === 'failed') {
    return (
      <div className="notice notice-danger" role="alert" aria-label="Hash chain check">
        <p>
          <strong>The hash chain of {check.fileName} could not be checked.</strong>
        </p>
        <p>{check.error instanceof Error ? check.error.message : 'The file could not be read.'}</p>
      </div>
    );
  }
  const verdict = chainVerdict(check.fileName, check.report, check.rowFilters);
  return (
    <div
      className={`notice notice-${verdict.tone}`}
      role={verdict.tone === 'danger' ? 'alert' : 'status'}
      aria-label="Hash chain check"
    >
      <p>
        <strong>{verdict.headline}</strong>
      </p>
      {verdict.lines.map((line) => (
        <p key={line}>{line}</p>
      ))}
      {verdict.lastHash ? (
        <p>
          Last hash <code>{verdict.lastHash}</code>
        </p>
      ) : null}
      <p className="muted">
        Checked in this browser, in {check.fileName} as saved. <code>ssc audit verify</code> makes the same
        check.
      </p>
    </div>
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
        ) : killSwitchApp(e) ? (
          <Link
            to="/apps/$appId/kill-switch/$runId"
            params={{ appId: killSwitchApp(e) ?? '', runId: e.target.id }}
          >
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
