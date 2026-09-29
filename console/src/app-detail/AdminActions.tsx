import { useRouteContext } from '@tanstack/react-router';
import { type FormEvent, useState } from 'react';
import {
  type AppOut,
  enableApp,
  type KillSwitchMode,
  type KillSwitchRun,
  POLL_MS,
  pullKillSwitch,
  transferOwner,
} from '../api/lifecycle';
import { findPeople, type Match, USER_ID_PATTERN } from '../api/directory';
import { Badge, type Tone } from '../components/Badge';
import { Button } from '../components/Button';
import { ConfirmAction } from '../components/ConfirmAction';
import { Dialog } from '../components/Dialog';
import { ProblemNotice } from '../components/ProblemNotice';
import { type Column, Table } from '../components/Table';
import { Lookup } from './Lookup';

type Step = KillSwitchRun['steps'][number];

const MODE_TEXT: Readonly<Record<KillSwitchMode, { label: string; body: string }>> = {
  disable: {
    label: 'Disable',
    body: 'Nobody can open it, its instances stop and its timers pause. Its sharing stays as it is.',
  },
  quarantine: {
    label: 'Quarantine',
    body: 'It stops as when disabled, and its sharing rules are frozen until it is enabled again.',
  },
};

const STEP_TEXT: Readonly<Record<Step['name'], string>> = {
  gateway_deny: 'Deny at the gateway',
  datagw_suspend: 'Suspend data connections',
  egress_remove: 'Remove internet access',
  scale_to_zero: 'Stop instances',
  pause_timers: 'Pause timers',
};

const STATE_TONE: Readonly<Record<string, Tone>> = {
  running: 'info',
  done: 'success',
  completed: 'success',
  unconfirmed: 'warning',
  failed: 'danger',
};

const STEP_COLUMNS: readonly Column<Step>[] = [
  { header: 'Step', cell: (s) => STEP_TEXT[s.name] },
  { header: 'State', cell: (s) => <Badge tone={STATE_TONE[s.state] ?? 'neutral'}>{s.state}</Badge> },
  { header: 'Took', cell: (s) => (s.elapsed_ms === null ? '…' : `${s.elapsed_ms} ms`) },
  { header: 'Tries', cell: (s) => s.attempts },
  { header: 'Last error', cell: (s) => (s.error ? <code>{s.error}</code> : '') },
];

/**
 * The admin-only controls: the kill switch (disable, quarantine), enable, and owner transfer.
 * Nothing shows until `whoami` has loaded; anyone but an admin sees a line instead. The API
 * refuses a non-admin either way.
 */
export function AdminActions({ app }: { readonly app: AppOut }) {
  const { queries, isAdmin } = useRouteContext({ from: '/_authed/apps/$appId' });
  const me = queries.useQuery('get', '/v1/whoami');
  const headingId = `admin-${app.id}`;
  return (
    <section className="panel" aria-labelledby={headingId}>
      <h2 id={headingId}>Admin actions</h2>
      {me.isPending ? (
        <p className="muted">Checking your role…</p>
      ) : me.isError ? (
        <ProblemNotice error={me.error} />
      ) : isAdmin(me.data) ? (
        <Controls app={app} />
      ) : (
        <p className="muted">Only an org admin can disable, quarantine, enable or transfer this app.</p>
      )}
    </section>
  );
}

function Controls({ app }: { readonly app: AppOut }) {
  const { api, queries, queryClient } = useRouteContext({ from: '/_authed/apps/$appId' });
  const [runId, setRunId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [enableError, setEnableError] = useState<unknown>(null);
  const [enabling, setEnabling] = useState(false);
  const appKey = queries.queryOptions('get', '/v1/apps/{app_id}', {
    params: { path: { app_id: app.id } },
  }).queryKey;

  async function pull(mode: KillSwitchMode) {
    const id = await pullKillSwitch(api, app.id, mode);
    setNotice(null);
    setEnableError(null);
    setRunId(id);
    await queryClient.invalidateQueries({ queryKey: appKey });
  }

  async function enable() {
    setEnabling(true);
    setEnableError(null);
    setNotice(null);
    try {
      queryClient.setQueryData(appKey, await enableApp(api, app.id));
      setRunId(null);
      setNotice(`${app.slug} is active again. Serving comes back once it restarts.`);
    } catch (e) {
      setEnableError(e);
      await queryClient.invalidateQueries({ queryKey: appKey });
    } finally {
      setEnabling(false);
    }
  }

  const modes: readonly KillSwitchMode[] =
    app.status === 'active' ? ['disable', 'quarantine'] : app.status === 'disabled' ? ['quarantine'] : [];

  return (
    <>
      {notice ? (
        <p className="notice notice-success" role="status">
          {notice}
        </p>
      ) : null}
      <div className="toolbar">
        {modes.map((mode) => (
          <ConfirmAction
            key={mode}
            label={MODE_TEXT[mode].label}
            title={`${MODE_TEXT[mode].label} ${app.slug}`}
            confirmText={app.slug}
            confirmLabel={MODE_TEXT[mode].label}
            onConfirm={() => pull(mode)}
          >
            <p>
              <strong>{app.slug}</strong> stops now. {MODE_TEXT[mode].body}
            </p>
          </ConfirmAction>
        ))}
        {app.status === 'active' ? null : (
          <Button variant="primary" onClick={() => void enable()} disabled={enabling}>
            Enable
          </Button>
        )}
        <TransferOwner app={app} />
      </div>
      {enableError ? <ProblemNotice error={enableError} /> : null}
      {runId ? <KillSwitchProgress appId={app.id} runId={runId} /> : null}
    </>
  );
}

function KillSwitchProgress({ appId, runId }: { readonly appId: string; readonly runId: string }) {
  const { queries } = useRouteContext({ from: '/_authed/apps/$appId' });
  const run = queries.useQuery(
    'get',
    '/v1/apps/{app_id}/kill-switch/{run_id}',
    { params: { path: { app_id: appId, run_id: runId } } },
    { refetchInterval: (q) => (q.state.data?.state === 'running' ? POLL_MS : false) },
  );
  if (run.isPending) return <p className="muted">Reading the kill switch run…</p>;
  if (run.isError) return <ProblemNotice error={run.error} />;
  const r = run.data;
  return (
    <div className="stack" aria-label="Kill switch run" role="group">
      <p aria-live="polite">
        {r.mode === 'disable' ? 'Disable' : 'Quarantine'} <code className="muted">{r.run_id}</code>{' '}
        <Badge tone={STATE_TONE[r.state] ?? 'neutral'}>{r.state}</Badge>
        {r.total_ms === null ? null : <span className="muted"> in {r.total_ms} ms</span>}
      </p>
      <Table
        caption="Kill switch steps"
        columns={STEP_COLUMNS}
        rows={r.steps}
        rowKey={(s) => s.name}
        empty="No step has started yet."
      />
    </div>
  );
}

function TransferOwner({ app }: { readonly app: AppOut }) {
  const { api, queries, queryClient } = useRouteContext({ from: '/_authed/apps/$appId' });
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<Match | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState<string | null>(null);

  function close() {
    setOpen(false);
    setPicked(null);
    setError(null);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!picked || busy) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await transferOwner(api, app.id, picked.id);
      queryClient.setQueryData(
        queries.queryOptions('get', '/v1/apps/{app_id}', { params: { path: { app_id: app.id } } })
          .queryKey,
        updated,
      );
      setDone(`${app.slug} now belongs to ${picked.label}.`);
      close();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button onClick={() => setOpen(true)}>Transfer ownership</Button>
      {done ? (
        <p className="notice notice-success" role="status">
          {done}
        </p>
      ) : null}
      <Dialog open={open} title={`Transfer ${app.slug}`} onClose={close}>
        <form className="stack" onSubmit={submit} aria-label={`Transfer ${app.slug}`}>
          <p>
            The new owner must be an active member of the organisation. The app's sharing stays as
            it is.
          </p>
          <Lookup
            label="New owner: email or usr_ id"
            placeholder="name@example.com"
            idPattern={USER_ID_PATTERN}
            search={(email) => findPeople(api, email)}
            picked={picked}
            onPick={setPicked}
          />
          {error ? <ProblemNotice error={error} /> : null}
          <div className="actions">
            <Button onClick={close}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={!picked || busy}>
              Transfer
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
