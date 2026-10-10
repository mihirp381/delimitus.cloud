import { createFileRoute, useRouter } from '@tanstack/react-router';
import { lazy, Suspense, useState } from 'react';
import { safeNext, signInMessage } from '../auth/oauth';
import { Brand, BrandRule } from '../components/Brand';
import { Button } from '../components/Button';

// The same test as DEV_LOGIN in ../auth/flags, written out so the bundler sees a literal `false`
// in a production build without VITE_SSC_DEV_LOGIN=1 and never emits the DevLogin chunk. An
// imported constant is folded too late: the chunk would still be written.
const DevLogin =
  import.meta.env.DEV || __SSC_DEV_LOGIN__ ? lazy(() => import('../auth/DevLogin')) : null;

export const Route = createFileRoute('/login')({
  validateSearch: (search: Record<string, unknown>): { next?: string } => {
    const next = safeNext(search.next);
    return next ? { next } : {};
  },
  component: LoginPage,
});

function LoginPage() {
  const { api, session, oauth } = Route.useRouteContext();
  // The router passes unvalidated search keys through, so the check is repeated here.
  const next = safeNext(Route.useSearch().next);
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function signIn() {
    setBusy(true);
    setError(null);
    try {
      await oauth.signIn(next ?? '/');
    } catch (e) {
      setError(signInMessage(e));
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <div className="login-card">
        <BrandRule />
        <span className="brand">
          <Brand />
        </span>
        <h1>Sign in to SSC</h1>
        <p className="lede">Use your work account. You come back here when you are signed in.</p>
        {error ? (
          <div className="notice notice-danger" role="alert">
            <p>{error}</p>
          </div>
        ) : null}
        <div>
          <Button variant="primary" className="btn-lg" disabled={busy} onClick={() => void signIn()}>
            Continue with your work account
          </Button>
        </div>
        {DevLogin ? (
          <Suspense fallback={<p className="muted">Loading…</p>}>
            <DevLogin api={api} session={session} onSignedIn={() => router.history.push(next ?? '/')} />
          </Suspense>
        ) : null}
      </div>
    </main>
  );
}
