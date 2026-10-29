import { useRouteContext } from '@tanstack/react-router';
import { type FormEvent, useState } from 'react';
import {
  type AppOut,
  deleteApp,
  enableApp,
  type KillSwitchMode,
  pullKillSwitch,
  transferOwner,
} from '../api/lifecycle';
import type { Match } from '../api/directory';
import { Badge } from '../components/Badge';
import { Button } from '../components/Button';
import { ConfirmAction } from '../components/ConfirmAction';
import { Dialog } from '../components/Dialog';
import { Panel } from '../components/Panel';
import { ProblemNotice } from '../components/ProblemNotice';
import { useToast } from '../components/Toast';
import { DeleteProgress } from './DeleteRun';
import { KillSwitchProgress } from './KillSwitchRun';
import { OwnerPicker } from './OwnerPicker';

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

/**
 * The admin-only controls: the kill switch (disable, quarantine), enable, owner transfer (or
 * assigning one to an app with no active owner), and deleting the app. Nothing shows until
 * `whoami` has loaded; anyone but an admin sees a line instead. The API refuses a non-admin either
 * way.
 */
export function AdminActions({ app }: { readonly app: AppOut }) {
  const { queries, isAdmin } = useRouteContext({ from: '/_authed/apps/$appId' });
  const me = queries.useQuery('get', '/v1/whoami');
  return (
    <Panel title="Admin actions" actions={<Badge tone="danger">Danger zone</Badge>} className="danger-zone">
      <p className="muted">
        These stop the app for everyone who uses it, hand it to someone else, or delete it for good.
      </p>
      {me.isPending ? (
        <p className="muted">Checking your role…</p>
      ) : me.isError ? (
        <ProblemNotice error={me.error} />
      ) : isAdmin(me.data) ? (
        <Controls app={app} />
      ) : (
        <p className="muted">
          Only an org admin can disable, quarantine, enable, transfer or delete this app.
        </p>
      )}
    </Panel>
  );
}

function Controls({ app }: { readonly app: AppOut }) {
  const { api, queries, queryClient } = useRouteContext({ from: '/_authed/apps/$appId' });
  const [runId, setRunId] = useState<string | null>(null);
  const [deleteRunId, setDeleteRunId] = useState<string | null>(null);
  const toast = useToast();
  const [enableError, setEnableError] = useState<unknown>(null);
  const [enabling, setEnabling] = useState(false);
  const appKey = queries.queryOptions('get', '/v1/apps/{app_id}', {
    params: { path: { app_id: app.id } },
  }).queryKey;

  /** A refusal may mean the app changed under us (another admin's run), so it is read again. */
  async function pull(mode: KillSwitchMode) {
    try {
      const id = await pullKillSwitch(api, app.id, mode);
      setEnableError(null);
      setRunId(id);
    } finally {
      await queryClient.invalidateQueries({ queryKey: appKey });
    }
  }

  /** The app is `deleting` from the moment the API answers, so it is read again either way. */
  async function remove() {
    try {
      const id = await deleteApp(api, app.id, app.slug);
      setEnableError(null);
      setRunId(null);
      setDeleteRunId(id);
    } finally {
      await queryClient.invalidateQueries({ queryKey: appKey });
    }
  }

  async function enable() {
    setEnabling(true);
    setEnableError(null);
    try {
      queryClient.setQueryData(appKey, await enableApp(api, app.id));
      setRunId(null);
      toast(`${app.slug} is active again. Serving comes back once it restarts.`);
    } catch (e) {
      setEnableError(e);
      await queryClient.invalidateQueries({ queryKey: appKey });
    } finally {
      setEnabling(false);
    }
  }

  const modes: readonly KillSwitchMode[] =
    app.status === 'active' ? ['disable', 'quarantine'] : app.status === 'disabled' ? ['quarantine'] : [];
  // An app being deleted takes no other change; deleting it again continues a run that stopped.
  const deleting = app.status === 'deleting';

  return (
    <>
      {deleting ? (
        <p>
          <strong>{app.slug}</strong> is being deleted. If a step failed, delete it again to
          continue from there.
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
        {app.status === 'active' || deleting ? null : (
          <Button variant="primary" onClick={() => void enable()} disabled={enabling}>
            Enable
          </Button>
        )}
        {deleting ? null : <TransferOwner app={app} />}
        <ConfirmAction
          label={deleting ? 'Continue deleting' : 'Delete app'}
          title={`Delete ${app.slug}`}
          confirmText={app.slug}
          confirmLabel="Delete"
          onConfirm={remove}
        >
          <p>
            <strong>{app.slug}</strong> stops now and is removed for good, in production and
            preview: its services and address, its databases, its files, its secrets, its timers
            and its sharing. This cannot be undone. The audit log keeps every row about it.
          </p>
        </ConfirmAction>
      </div>
      {enableError ? <ProblemNotice error={enableError} /> : null}
      {runId ? <KillSwitchProgress appId={app.id} runId={runId} /> : null}
      {deleteRunId ? <DeleteProgress appId={app.id} runId={deleteRunId} /> : null}
    </>
  );
}

function TransferOwner({ app }: { readonly app: AppOut }) {
  const { api, queries, queryClient } = useRouteContext({ from: '/_authed/apps/$appId' });
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<Match | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const toast = useToast();
  const assign = app.owner_state !== 'active';
  const title = assign ? `Assign an owner to ${app.slug}` : `Transfer ${app.slug}`;

  function show() {
    setPicked(null);
    setError(null);
    setOpen(true);
  }

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
      toast(`${app.slug} now belongs to ${picked.label}.`);
      close();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button onClick={show}>{assign ? 'Assign owner' : 'Transfer ownership'}</Button>
      <Dialog open={open} title={title} onClose={close}>
        <form className="stack" onSubmit={submit} aria-label={title}>
          <p>
            {assign ? `${app.owner_name} is no longer active. ` : null}The new owner must be an active
            member of the organisation. The app's sharing stays as it is.
          </p>
          <OwnerPicker current={app.owner_user_id} picked={picked} onPick={setPicked} />
          {error ? <ProblemNotice error={error} /> : null}
          <div className="actions">
            <Button onClick={close}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={!picked || busy}>
              {assign ? 'Assign' : 'Transfer'}
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
