import type { UserMatch } from '../api/people';
import { exact } from '../components/Ago';
import { Badge } from '../components/Badge';
import { Button } from '../components/Button';
import { Id } from '../components/Copy';

export function RoleBadge({ role }: { readonly role: UserMatch['role'] }) {
  return <Badge tone={role === 'admin' ? 'info' : 'neutral'}>{role}</Badge>;
}

export function StateBadge({ status }: { readonly status: UserMatch['status'] }) {
  return <Badge tone={status === 'active' ? 'success' : 'neutral'}>{status}</Badge>;
}

export function PersonDetail({ person, onClose }: { readonly person: UserMatch; readonly onClose: () => void }) {
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
      <p className="muted">To change a person&apos;s name, role or state, change them in your identity provider.</p>
      <div className="actions">
        <Button variant="primary" onClick={onClose}>
          Close
        </Button>
      </div>
    </>
  );
}
