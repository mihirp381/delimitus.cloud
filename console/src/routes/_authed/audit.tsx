import { useInfiniteQuery } from '@tanstack/react-query';
import { createFileRoute, Link, useNavigate } from '@tanstack/react-router';
import { type ChangeEvent, type FormEvent, useId, useRef, useState } from 'react';
import {
  ACTOR_KINDS,
  AUDIT_ACTIONS,
  type AuditEvent,
  type AuditFilters,
  auditFilters,
  type ExportFormat,
  exportAudit,
  isDeleteRun,
  killSwitchApp,
} from '../../api/audit';
import { must } from '../../api/client';
import { type ChainReport, chainVerdict, checkChain } from '../../auditChain';
import { AdminOnly } from '../../components/AdminOnly';
import { Ago, exact } from '../../components/Ago';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { Id } from '../../components/Copy';
import { Dialog } from '../../components/Dialog';
import { FilterBar } from '../../components/FilterBar';
import { Loading } from '../../components/Loading';
import { Menu } from '../../components/Menu';
import { Notice } from '../../components/Notice';
import { PageHeader } from '../../components/PageHeader';
import { Person } from '../../components/Person';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';
import { useToast } from '../../components/Toast';
import { copyMessage, useClipboard } from '../../components/useClipboard';
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

function AuditPage() {
  return (
    <AdminOnly title="Audit log" refusal="Only org admins can see the audit log.">
      <AuditLog />
    </AdminOnly>
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
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<unknown>(null);
  const toast = useToast();
  const [check, setCheck] = useState<Check | null>(null);
  const [search, setSearch] = useState('');
  const [open, setOpen] = useState<AuditEvent | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

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
    setExporting(true);
    setExportError(null);
    setCheck(null);
    try {
      const { blob, fileName } = await exportAudit(api, filters, format);
      // The browser saves the API's bytes as they came; the check below reads the same bytes.
      saveBlob(blob, fileName);
      toast(`Saved ${fileName}.`);
      if (format === 'jsonl') {
        await verify(blob, fileName, ROW_FILTERS.some((name) => filters[name] !== undefined));
      }
      await queryClient.invalidateQueries({ queryKey: ['audit'] });
    } catch (error) {
      setExportError(error);
    } finally {
      setExporting(false);
    }
  }

  function verifyFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    // Emptied so that choosing the same file again checks it again.
    event.currentTarget.value = '';
    if (!file) return;
    setExportError(null);
    void verify(file, file.name, false);
  }

  const rows = events.data?.pages.flatMap((p) => p.events) ?? [];
  const query = search.trim().toLowerCase();
  const shown = query ? rows.filter((e) => matches(e, query)) : rows;
  const hint = useId();
  const columns = eventColumns(setOpen);

  return (
    <>
      <PageHeader
        title="Audit log"
        purpose="What happened in your organisation, newest first."
        about={
          <p>
            An export holds every event matching the applied filters, oldest first, and is itself recorded as{' '}
            <code>audit.exported</code>. A JSON Lines export carries the log&apos;s hash chain, which is checked
            here in your browser once it is saved.
          </p>
        }
      >
        <Menu
          label={exporting ? 'Exporting…' : 'Export'}
          disabled={exporting}
          items={[
            { label: 'Export CSV', onSelect: () => void download('csv') },
            { label: 'Export JSON Lines', onSelect: () => void download('jsonl') },
            { label: 'Verify a file…', onSelect: () => fileInput.current?.click() },
          ]}
        />
        <input
          ref={fileInput}
          type="file"
          className="visually-hidden"
          tabIndex={-1}
          aria-label="Verify a file"
          accept=".jsonl,.ndjson,application/x-ndjson"
          aria-describedby={hint}
          onChange={verifyFile}
        />
        <span id={hint} className="visually-hidden">
          Checks the hash chain of a JSON Lines export you saved earlier. The file stays on this computer.
        </span>
      </PageHeader>
      {exportError ? <ProblemNotice error={exportError} /> : null}
      {check ? <ChainNotice check={check} /> : null}
      <FilterForm
        key={JSON.stringify(filters)}
        filters={filters}
        search={search}
        onSearch={setSearch}
        onApply={(next) => void navigate({ search: next })}
      />
      {events.data ? (
        <section className="panel">
          <Table
            caption="Audit events"
            columns={columns}
            rows={shown}
            rowKey={(e) => String(e.seq)}
            onOpen={setOpen}
            empty={Object.keys(filters).length || query ? 'No event matches the filters.' : 'No events yet.'}
            emptyHint={
              Object.keys(filters).length || query
                ? 'Widen or clear the filters to see more.'
                : 'Every change made in your organisation is recorded here as it happens.'
            }
          />
          <div className="more">
            <p className="muted count" aria-live="polite">
              {query ? `${shown.length} of ` : ''}
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
        <Loading label="Loading events…" rows={3} />
      )}
      <Dialog open={open !== null} title={open ? `Event ${open.seq}` : 'Event'} onClose={() => setOpen(null)}>
        {open ? <EventDetail event={open} onClose={() => setOpen(null)} /> : null}
      </Dialog>
    </>
  );
}

function matches(e: AuditEvent, query: string): boolean {
  return [e.action, e.actor.name, e.actor.email, e.target.kind, e.target.name].some((v) =>
    v?.toLowerCase().includes(query),
  );
}

/** What the chain check found in one file, in the words `ssc audit verify` uses. */
function ChainNotice({ check }: { readonly check: Check }) {
  if (check.state === 'checking') {
    return (
      <Notice tone="info" aria-label="Hash chain check">
        <p>Checking the hash chain of {check.fileName}…</p>
      </Notice>
    );
  }
  if (check.state === 'failed') {
    return (
      <Notice tone="danger" aria-label="Hash chain check" title={`The hash chain of ${check.fileName} could not be checked.`}>
        <p>{check.error instanceof Error ? check.error.message : 'The file could not be read.'}</p>
      </Notice>
    );
  }
  const verdict = chainVerdict(check.fileName, check.report, check.rowFilters);
  return (
    <Notice
      tone={verdict.tone === 'warning' ? 'warn' : verdict.tone}
      aria-label="Hash chain check"
      title={verdict.headline}
    >
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
    </Notice>
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
  search,
  onSearch,
  onApply,
}: {
  readonly filters: AuditFilters;
  readonly search: string;
  readonly onSearch: (value: string) => void;
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
    <form className="panel" aria-label="Filter events" onSubmit={submit}>
      <FilterBar
        label="Filters"
        count={ROW_FILTERS.filter((name) => filters[name] !== undefined).length}
        primary={
          <>
            <label className="field" htmlFor={field('search')}>
              <span>Search</span>
              <input
                id={field('search')}
                type="search"
                placeholder="Action, person or target"
                value={search}
                onChange={(e) => onSearch(e.target.value)}
              />
            </label>
            <label className="field" htmlFor={field('since')}>
              <span>From (inclusive)</span>
              <input id={field('since')} name="since" type="datetime-local" defaultValue={localInput(filters.since)} />
            </label>
            <label className="field" htmlFor={field('until')}>
              <span>Until (exclusive)</span>
              <input id={field('until')} name="until" type="datetime-local" defaultValue={localInput(filters.until)} />
            </label>
            <Button type="submit" variant="primary">
              Search
            </Button>
            <Button onClick={() => onApply({})}>Clear filters</Button>
          </>
        }
        more={
          <div className="filters">
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
              <input id={field('actor_id')} name="actor_id" maxLength={200} defaultValue={filters.actor_id ?? ''} />
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
              <input id={field('target_id')} name="target_id" maxLength={200} defaultValue={filters.target_id ?? ''} />
            </label>
          </div>
        }
      />
    </form>
  );
}

function EventDetail({ event, onClose }: { readonly event: AuditEvent; readonly onClose: () => void }) {
  const text = JSON.stringify(event, null, 2);
  const { state, copy } = useClipboard();
  return (
    <>
      <dl className="facts">
        <dt>When</dt>
        <dd>
          <time dateTime={event.at}>{exact(event.at)}</time>
        </dd>
        <dt>Action</dt>
        <dd>
          <code>{event.action}</code>
        </dd>
        <dt>Actor</dt>
        <dd>
          <Person name={event.actor.name} email={event.actor.email} /> <Id value={event.actor.id} label="actor id" />
        </dd>
        <dt>Target</dt>
        <dd>
          {event.target.kind} {event.target.name} <Id value={event.target.id} label="target id" />
        </dd>
        {event.policy_decision_id !== null ? (
          <>
            <dt>Policy decision</dt>
            <dd>
              <Id value={event.policy_decision_id} label="policy decision id" />
            </dd>
          </>
        ) : null}
        <dt>Seq</dt>
        <dd>{event.seq}</dd>
      </dl>
      <pre className="event-json">{text}</pre>
      <div className="actions">
        {state !== 'idle' ? (
          <span className="muted" role="status">
            {copyMessage(state, 'text')}
          </span>
        ) : null}
        <Button onClick={() => void copy(text)}>Copy JSON</Button>
        <Button variant="primary" onClick={onClose}>
          Close
        </Button>
      </div>
    </>
  );
}

function eventColumns(open: (e: AuditEvent) => void): readonly Column<AuditEvent>[] {
  return [
    { header: 'Time', className: 'nowrap', cell: (e) => <Ago at={e.at} /> },
    {
      header: 'Actor',
      cell: (e) => (
        <>
          <Person name={e.actor.name} email={e.actor.email} />
          {e.actor.via_agent ? (
            <>
              {' '}
              <Badge tone="info">agent</Badge>
            </>
          ) : null}
        </>
      ),
    },
    {
      header: 'Action',
      cell: (e) => (
        <button type="button" className="link-btn" aria-haspopup="dialog" onClick={() => open(e)}>
          <code>{e.action}</code>
        </button>
      ),
    },
    {
      header: 'Target',
      cell: (e) => {
        const app = killSwitchApp(e);
        return (
          <>
            {e.target.kind}{' '}
            {e.target.kind === 'app' ? (
              <Link to="/apps/$appId" params={{ appId: e.target.id }}>
                {e.target.name ?? 'Open'}
              </Link>
            ) : app ? (
              <Link
                to={isDeleteRun(e) ? '/apps/$appId/delete/$runId' : '/apps/$appId/kill-switch/$runId'}
                params={{ appId: app, runId: e.target.id }}
              >
                Open run
              </Link>
            ) : (
              e.target.name
            )}
          </>
        );
      },
    },
  ];
}
