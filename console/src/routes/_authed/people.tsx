import { createFileRoute } from '@tanstack/react-router';
import { AdminOnly } from '../../components/AdminOnly';
import { PageHeader } from '../../components/PageHeader';
import { Roster } from '../../people/Roster';
import { UnmatchedLogins } from '../../people/UnmatchedLogins';

export const Route = createFileRoute('/_authed/people')({
  component: PeoplePage,
});

function PeoplePage() {
  return (
    <AdminOnly title="People" refusal="Only org admins can look people up and link logins.">
      <PageHeader
        title="People"
        purpose="Everyone your directory has synced, with their role and last sign-in."
        about={<p>Add, change or remove people and groups in your identity provider, and SSC follows.</p>}
      />
      <Roster />
      <UnmatchedLogins />
    </AdminOnly>
  );
}
