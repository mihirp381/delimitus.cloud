import { useRouteContext } from '@tanstack/react-router';
import { useState } from 'react';
import { deactivatePerson, reactivatePerson, type UserMatch } from '../api/people';
import { exact } from '../components/Ago';
import { AdminOnly } from '../components/AdminOnly';
import { Badge, type Tone } from '../components/Badge';
import { Button } from '../components/Button';
import { ConfirmAction } from '../components/ConfirmAction';
import { Id } from '../components/Copy';
import { ProblemNotice } from '../components/ProblemNotice';
import { useToast } from '../components/Toast';
import { PersonApps } from './PersonApps';

const STATE_TONE: Readonly<Record<UserMatch['status'], Tone>> = {
  active: 'success',
  invited: 'info',
  deactivated: 'neutral',
};

export function RoleBadge({ role }: { readonly role: UserMatch['role'] }) {
  return <Badge tone={role === 'admin' ? 'info' : 'neutral'}>{role}</Badge>;
}

export function StateBadge({ status }: { readonly status: UserMatch['status'] }) {
  return <Badge tone={STATE_TONE[status]}>{status}</Badge>;
}

interface Props {
  readonly person: UserMatch;
  readonly social: boolean;
  readonly onChange: (person: UserMatch) => void;
  readonly onClose: () => void;
}

export function PersonDetail({ person, social, onChange, onClose }: Props) {
  return (
    <>
      <dl className="facts">
        <dt>Email</dt>
        <dd>{person.email}</dd>
        <dt>Role</dt>
        <dd>
          <RoleBadge role={person.role} />
        </dd>
        <dt>State</dt>
        <dd>
          <StateBadge status={person.status} />
        </dd>
        <dt>Last sign-in</dt>
        <dd>
          {person.last_sign_in_at ? (
            <time dateTime={person.last_sign_in_at}>{exact(person.last_sign_in_at)}</time>
          ) : (
            'Never'
          )}
        </dd>
        <dt>Added</dt>
        <dd>
          <time dateTime={person.created_at}>{exact(person.created_at)}</time>
        </dd>
        <dt>Id</dt>
        <dd>
          <Id value={person.id} label="person id" />
        </dd>
      </dl>
      <PersonApps person={person} />
      {social ? (
        <AdminOnly quiet>
          <StateActions person={person} onChange={onChange} />
        </AdminOnly>
      ) : (
        <p className="muted">To change a person&apos;s name, role or state, change them in your identity provider.</p>
      )}
      <div className="actions">
        <Button variant="primary" onClick={onClose}>
          Close
        </Button>
      </div>
    </>
  );
}

function StateActions({ person, onChange }: { readonly person: UserMatch; readonly onChange: (p: UserMatch) => void }) {
  const { api, queryClient } = useRouteContext({ from: '/_authed' });
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const name = person.display_name || person.email;

  async function done(changed: UserMatch, message: string) {
    onChange(changed);
    toast(message);
    await queryClient.invalidateQueries({ queryKey: ['users'] });
  }

  async function deactivate() {
    await done(await deactivatePerson(api, person.id), `Deactivated ${name}. Their sessions have ended.`);
  }

  async function reactivate() {
    setBusy(true);
    setError(null);
    try {
      await done(await reactivatePerson(api, person.id), `Reactivated ${name}. They can sign in again.`);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="toolbar">
        {person.status === 'deactivated' ? (
          <Button onClick={() => void reactivate()} disabled={busy}>
            Reactivate
          </Button>
        ) : (
          <ConfirmAction label="Deactivate" title={`Deactivate ${name}`} confirmLabel="Deactivate" onConfirm={deactivate}>
            <p>
              <strong>{name}</strong> loses access now: every console, browser and command-line
              session ends, and apps refuse them on their next request. You can reactivate them
              later.
            </p>
          </ConfirmAction>
        )}
      </div>
      {error ? <ProblemNotice error={error} /> : null}
    </>
  );
}
