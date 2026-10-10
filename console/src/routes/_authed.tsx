import { createFileRoute, Link, Outlet, redirect, useLocation, useNavigate } from '@tanstack/react-router';
import { Brand, BrandRule } from '../components/Brand';
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
  // An app's own page belongs to "Apps": the link reads as current there too. Looks only; it goes where it did.
  const inApp = useLocation({ select: (location) => location.pathname.startsWith('/apps/') });

  function signOut() {
    // Forgets the tokens at once; the auth host's /revoke ends the session behind them.
    void session.signOut();
    queryClient.clear();
    void navigate({ to: '/login' });
  }

  return (
    <>
      <header className="topbar">
        <div className="topbar-in">
          <Link to="/" className="brand">
            <Brand />
          </Link>
          <nav aria-label="Main">
            <Link
              to="/"
              activeOptions={{ exact: true }}
              className={inApp ? 'active' : undefined}
              aria-current={inApp ? 'true' : undefined}
            >
              Apps
            </Link>
            <Link to="/approvals">Approvals</Link>
            <Link to="/connections">Connections</Link>
            <Link to="/egress">Internet access</Link>
            {isAdmin(me.data) ? <Link to="/environment">Your environment</Link> : null}
            {isAdmin(me.data) ? <Link to="/audit">Audit log</Link> : null}
          </nav>
          <div className="topbar-user">
            {me.data ? (
              <span className="person">
                <span className="avatar" aria-hidden="true">
                  <svg viewBox="0 0 16 16" fill="currentColor" focusable="false">
                    <circle cx="8" cy="5.2" r="2.9" />
                    <path d="M2.4 14a5.6 5.6 0 0 1 11.2 0 .6.6 0 0 1-.6.6H3a.6.6 0 0 1-.6-.6Z" />
                  </svg>
                </span>
                <span className="muted person-name">
                  Signed in as <code>{me.data.subject}</code>
                </span>
              </span>
            ) : null}
            <Button className="btn-sm" onClick={signOut}>
              Sign out
            </Button>
          </div>
        </div>
        <BrandRule />
      </header>
      <main className="page">
        <Outlet />
      </main>
    </>
  );
}
