import { createFileRoute } from '@tanstack/react-router';
import { AdminOnly } from '../../components/AdminOnly';
import { PageHeader } from '../../components/PageHeader';
import { InvitePerson } from '../../people/InvitePerson';
import { Roster } from '../../people/Roster';
import { useSignInMode } from '../../people/signInMode';
import { UnmatchedLogins } from '../../people/UnmatchedLogins';

export const Route = createFileRoute('/_authed/people')({
  component: PeoplePage,
});

function PeoplePage() {
  const social = useSignInMode() === 'social';
  return (
    <AdminOnly title="People" refusal="Only org admins can look people up and link logins.">
      <PageHeader
        title="People"
        purpose={
          social
            ? 'Everyone invited to your organisation, with their role and last sign-in.'
            : 'Everyone your directory has synced, with their role and last sign-in.'
        }
        about={
          social ? (
            <p>Invite people by email. Deactivate someone here and their access ends on their next request.</p>
          ) : (
            <p>Add, change or remove people and groups in your identity provider, and SSC follows.</p>
          )
        }
      >
        {social ? <InvitePerson /> : null}
      </PageHeader>
      <Roster social={social} />
      {social ? null : <UnmatchedLogins />}
    </AdminOnly>
  );
}
