import { createFileRoute, Link, Outlet, redirect, useNavigate } from '@tanstack/react-router';
import { Button } from '../components/Button';

export const Route = createFileRoute('/_authed')({
  beforeLoad: ({ context, location }) => {
    if (!context.session.token()) {
      throw redirect({ to: '/login', search: { next: location.href } });
    }
  },
  component: AuthedLayout,
});

function AuthedLayout() {
  const { queries, session, queryClient, isAdmin } = Route.useRouteContext();
  const navigate = useNavigate();
  const me = queries.useQuery('get', '/v1/whoami');

  function signOut() {
    // Forgets the tokens at once; the auth host's /revoke ends the session behind them.
    void session.signOut();
    queryClient.clear();
    void navigate({ to: '/login' });
  }

  return (
    <>
      <header className="topbar">
        <Link to="/" className="brand">
          SSC console
        </Link>
        <nav aria-label="Main">
          <Link to="/" activeOptions={{ exact: true }}>
            Apps
          </Link>
          <Link to="/approvals">Approvals</Link>
          <Link to="/connections">Connections</Link>
          <Link to="/egress">Internet access</Link>
          {isAdmin(me.data) ? <Link to="/environment">Your environment</Link> : null}
          {isAdmin(me.data) ? <Link to="/audit">Audit log</Link> : null}
        </nav>
        <span className="spacer" />
        {me.data ? (
          <span className="muted">
            Signed in as <code>{me.data.subject}</code>
          </span>
        ) : null}
        <Button onClick={signOut}>Sign out</Button>
      </header>
      <main className="page">
        <Outlet />
      </main>
    </>
  );
}
