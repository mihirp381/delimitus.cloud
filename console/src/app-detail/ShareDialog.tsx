import { useRouteContext } from '@tanstack/react-router';
import { type FormEvent, useId, useRef, useState } from 'react';
import { askCeilingApproval } from '../api/connections';
import {
  findGroups,
  findPeople,
  GROUP_ID_PATTERN,
  type Match,
  USER_ID_PATTERN,
} from '../api/directory';
import {
  askShareApproval,
  type Grant,
  type GrantInput,
  type GrantsSnapshot,
  type GrantsUpdate,
  readShareRequirements,
  type ShareRequirement,
  updateGrants,
  withGrant,
} from '../api/grants';
import type { AppOut, EnvironmentOut } from '../api/lifecycle';
import { ApiProblem } from '../api/problem';
import { Button } from '../components/Button';
import { Dialog } from '../components/Dialog';
import { Notice } from '../components/Notice';
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

/** A change refused with APPROVAL_REQUIRED: the grants it would have set, and what it needs. */
interface Needed {
  readonly grants: readonly (Grant | GrantInput)[];
  readonly requirements: readonly ShareRequirement[];
}

/** An approval request for the refused change: opened here, or already pending. */
interface Asked {
  readonly id: string;
  readonly kind: string;
  readonly connection: string | null;
  readonly state: string;
}

/** What one requirement asks, and who decides it. */
function needs(r: { readonly kind: string; readonly connection?: string | null }): string {
  switch (r.kind) {
    case 'widen_audience':
      return 'widen the audience of an app that reaches company data (another org admin decides)';
    case 'exceed_ceiling':
      return `exceed the ceiling of connection ${r.connection ?? '?'} (its owner or an org admin decides)`;
    case 'agent_share':
      return 'a change made through an agent (another org admin decides)';
    default:
      return r.kind;
  }
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
  const [needed, setNeeded] = useState<Needed | null>(null);
  const [asked, setAsked] = useState<readonly Asked[] | null>(null);
  /** Bumped whenever the change being made is forgotten, so a late answer is dropped. */
  const generation = useRef(0);
  const roleId = useId();
  const title = `Share ${ENV_TITLE[env.name].toLowerCase()}`;

  /** Forgets a refusal and an approval request, which were for the grant set as it was. */
  function forget() {
    generation.current += 1;
    setError(null);
    setNeeded(null);
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
    setNeeded(null);
    setAsked(null);
    const mine = generation.current;
    const target = { appId: app.id, environmentId: env.id };
    let sent: readonly (Grant | GrantInput)[] = [];
    const change = (grants: readonly Grant[]) => {
      sent = withGrant(subject)(grants);
      return sent;
    };
    try {
      const update = await updateGrants(api, target, change, seen);
      onDone(
        update,
        update.state === 'pending'
          ? `Waiting for approval, nothing changed yet: ${update.approvalIds.join(', ')}.`
          : `Shared ${ENV_TITLE[env.name].toLowerCase()} with ${whom} (${role}).`,
      );
      close();
    } catch (e) {
      setError(e);
      if (e instanceof ApiProblem && e.code === 'APPROVAL_REQUIRED') {
        // The refusal names nothing; the same grants, checked again, say what to ask for.
        try {
          const { requirements } = await readShareRequirements(api, target, sent);
          if (generation.current === mine) setNeeded({ grants: sent, requirements });
        } catch (read) {
          if (generation.current === mine) setError(read);
        }
      }
    } finally {
      setBusy(false);
    }
  }

  /** Opens one request per requirement not already pending, for exactly the refused grants. */
  async function ask() {
    if (!needed || busy) return;
    setBusy(true);
    const target = { appId: app.id, environmentId: env.id };
    const done: Asked[] = [];
    try {
      for (const r of needed.requirements) {
        const connection = r.connection ?? null;
        if (r.state === 'pending' && r.approval_id) {
          done.push({ id: r.approval_id, kind: r.kind, connection, state: 'pending' });
          continue;
        }
        const opened =
          r.kind === 'exceed_ceiling' && connection
            ? await askCeilingApproval(api, target, connection, needed.grants)
            : await askShareApproval(api, env.id, needed.grants);
        done.push({ id: opened.id, kind: r.kind, connection, state: opened.state });
      }
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      if (done.length > 0) setAsked(done);
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
          {needed && !asked ? (
            <Notice tone="info" role="note">
              {needed.requirements.length > 0 ? (
                <>
                  <p>This change needs approval before it applies:</p>
                  <ul>
                    {needed.requirements.map((r) => (
                      <li key={`${r.kind}:${r.connection ?? ''}`}>
                        {needs(r)}
                        {r.state === 'pending' && r.approval_id ? (
                          <>
                            {' '}
                            — already asked: <code>{r.approval_id}</code> (pending)
                          </>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                  <p>
                    Ask for it here, then share again once every request is approved; requests are
                    listed under Approvals.
                  </p>
                  <div>
                    <Button onClick={() => void ask()} disabled={busy}>
                      Ask for approval
                    </Button>
                  </div>
                </>
              ) : (
                <p>Every approval this change needs is now in. Share again.</p>
              )}
            </Notice>
          ) : null}
          {asked ? (
            <Notice tone="success">
              <p>Asked for approval. Share again once every request is approved:</p>
              <ul>
                {asked.map((a) => (
                  <li key={a.id}>
                    <code>{a.id}</code>: {needs(a)} ({a.state})
                  </li>
                ))}
              </ul>
            </Notice>
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
