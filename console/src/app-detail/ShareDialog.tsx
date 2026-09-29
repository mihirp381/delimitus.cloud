import { useRouteContext } from '@tanstack/react-router';
import { type FormEvent, useId, useState } from 'react';
import {
  findGroups,
  findPeople,
  GROUP_ID_PATTERN,
  type Match,
  USER_ID_PATTERN,
} from '../api/directory';
import {
  type Approval,
  askShareApproval,
  type Grant,
  type GrantInput,
  type GrantsSnapshot,
  type GrantsUpdate,
  updateGrants,
  withGrant,
} from '../api/grants';
import type { AppOut, EnvironmentOut } from '../api/lifecycle';
import { ApiProblem } from '../api/problem';
import { Button } from '../components/Button';
import { Dialog } from '../components/Dialog';
import { ProblemNotice } from '../components/ProblemNotice';
import { ENV_TITLE } from './names';
import { Lookup } from './Lookup';

type Kind = 'group' | 'user' | 'org';
type Role = GrantInput['role'];

interface Props {
  readonly app: AppOut;
  readonly env: EnvironmentOut;
  readonly seen: GrantsSnapshot | undefined;
  /** Called with the result and a sentence saying what happened. */
  readonly onDone: (update: GrantsUpdate, message: string) => void;
}

const KINDS: readonly { readonly kind: Kind; readonly label: string }[] = [
  { kind: 'group', label: 'A group' },
  { kind: 'user', label: 'A person' },
  { kind: 'org', label: 'Everyone in the organisation' },
];

/**
 * Shares one environment with a group, a person or the whole org. Groups are found by name by
 * anyone who may share. People are found by email only by org admins (the API keeps email
 * lookup to them); everyone else enters a `usr_` id.
 */
export function ShareDialog({ app, env, seen, onDone }: Props) {
  const { api, queries, isAdmin } = useRouteContext({ from: '/_authed/apps/$appId' });
  const me = queries.useQuery('get', '/v1/whoami');
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<Kind>('group');
  const [picked, setPicked] = useState<Match | null>(null);
  const [role, setRole] = useState<Role>(env.name === 'preview' ? 'builder' : 'user');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [needsApproval, setNeedsApproval] = useState<readonly (Grant | GrantInput)[] | null>(null);
  const [asked, setAsked] = useState<Approval | null>(null);
  const roleId = useId();
  const title = `Share ${ENV_TITLE[env.name].toLowerCase()}`;

  /** Forgets a refusal and an approval request, which were for the grant set as it was. */
  function forget() {
    setError(null);
    setNeedsApproval(null);
    setAsked(null);
  }

  function reset() {
    setPicked(null);
    forget();
  }

  function pick(match: Match | null) {
    setPicked(match);
    forget();
  }

  function show() {
    setKind('group');
    reset();
    setOpen(true);
  }

  function close() {
    setOpen(false);
    setKind('group');
    reset();
  }

  function pickRole(next: Role) {
    setRole(next);
    forget();
  }

  function pickKind(next: Kind) {
    setKind(next);
    reset();
  }

  const subject: GrantInput | null =
    kind === 'org'
      ? { role, subject_kind: 'org' }
      : picked
        ? { role, subject_kind: kind, subject_id: picked.id }
        : null;
  const whom = kind === 'org' ? 'everyone in the organisation' : (picked?.label ?? '');

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!subject || busy) return;
    setBusy(true);
    setError(null);
    setNeedsApproval(null);
    setAsked(null);
    let sent: readonly (Grant | GrantInput)[] = [];
    const change = (grants: readonly Grant[]) => {
      sent = withGrant(subject)(grants);
      return sent;
    };
    try {
      const update = await updateGrants(api, { appId: app.id, environmentId: env.id }, change, seen);
      onDone(
        update,
        update.state === 'pending'
          ? `Waiting for approval, nothing changed yet: ${update.approvalIds.join(', ')}.`
          : `Shared ${ENV_TITLE[env.name].toLowerCase()} with ${whom} (${role}).`,
      );
      close();
    } catch (e) {
      if (e instanceof ApiProblem && e.code === 'APPROVAL_REQUIRED') setNeedsApproval(sent);
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  async function ask() {
    if (!needsApproval || busy) return;
    setBusy(true);
    try {
      setAsked(await askShareApproval(api, env.id, needsApproval));
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  const admin = isAdmin(me.data);
  const personHint = me.isPending
    ? 'Checking your role…'
    : admin
      ? 'Type an email address and press Find, or paste a usr_ id.'
      : 'Only an org admin can look people up by email. Ask the person for their usr_ id.';

  return (
    <>
      <Button variant="primary" onClick={show} aria-label={`Share ${ENV_TITLE[env.name]}`}>
        Share
      </Button>
      <Dialog open={open} title={title} onClose={close}>
        <form className="stack" onSubmit={submit} aria-label={title}>
          <fieldset className="choices">
            <legend>Share with</legend>
            {KINDS.map((k) => (
              <label key={k.kind} className="check">
                <input
                  type="radio"
                  name={`kind-${env.id}`}
                  checked={kind === k.kind}
                  onChange={() => pickKind(k.kind)}
                />
                <span>{k.label}</span>
              </label>
            ))}
          </fieldset>
          {kind === 'group' ? (
            <Lookup
              key="group"
              label="Group name or grp_ id"
              placeholder="Finance team"
              idPattern={GROUP_ID_PATTERN}
              search={(name) => findGroups(api, name)}
              picked={picked}
              onPick={pick}
            />
          ) : null}
          {kind === 'user' ? (
            <>
              {me.isError ? <ProblemNotice error={me.error} /> : null}
              <Lookup
                key={admin ? 'user-email' : 'user-id'}
                label={admin ? 'Email or usr_ id' : 'User id'}
                placeholder={admin ? 'name@example.com' : 'usr_…'}
                idPattern={USER_ID_PATTERN}
                search={admin ? (email) => findPeople(api, email) : undefined}
                hint={personHint}
                picked={picked}
                onPick={pick}
              />
            </>
          ) : null}
          <label className="field" htmlFor={roleId}>
            <span>Role</span>
            <select id={roleId} value={role} onChange={(e) => pickRole(e.target.value as Role)}>
              {env.name === 'preview' ? null : <option value="user">user: can open the app</option>}
              <option value="builder">builder: can also ship and share it</option>
            </select>
          </label>
          {env.name === 'preview' ? <p className="muted">Preview is for builders only.</p> : null}
          {error ? <ProblemNotice error={error} /> : null}
          {needsApproval && !asked ? (
            <div className="notice notice-info" role="note">
              <p>
                Another admin of the organisation must approve exactly this change. Ask for it
                here, then share again once it is approved; the request is listed under Approvals.
              </p>
              <div>
                <Button onClick={() => void ask()} disabled={busy}>
                  Ask for approval
                </Button>
              </div>
            </div>
          ) : null}
          {asked ? (
            <p className="notice notice-success" role="status">
              Asked for approval: <code>{asked.id}</code> ({asked.state}). Share again once another
              admin has approved it.
            </p>
          ) : null}
          <div className="actions">
            <Button onClick={close}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={!subject || busy}>
              Share
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
