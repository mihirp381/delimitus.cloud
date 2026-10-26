import { createFileRoute, useRouter } from '@tanstack/react-router';
import { type FormEvent, lazy, Suspense, useState } from 'react';
import { isSignInHint, keepHint, NO_SIGN_IN_MESSAGE, readHint, safeNext, signInMessage } from '../auth/oauth';
import { Brand, BrandRule } from '../components/Brand';
import { Button } from '../components/Button';

// The same test as DEV_LOGIN in ../auth/flags, written out so the bundler sees a literal `false`
// in a production build without VITE_SSC_DEV_LOGIN=1 and never emits the DevLogin chunk. An
// imported constant is folded too late: the chunk would still be written.
const DevLogin =
  import.meta.env.DEV || __SSC_DEV_LOGIN__ ? lazy(() => import('../auth/DevLogin')) : null;

interface LoginSearch {
  next?: string;
  error?: 'no_sign_in';
}

export const Route = createFileRoute('/login')({
  validateSearch: (search: Record<string, unknown>): LoginSearch => {
    const next = safeNext(search.next);
    return {
      ...(next ? { next } : {}),
      ...(search.error === 'no_sign_in' ? { error: 'no_sign_in' as const } : {}),
    };
  },
  component: LoginPage,
});

function LoginPage() {
  const { api, session, oauth } = Route.useRouteContext();
  const search = Route.useSearch();
  // The router passes unvalidated search keys through, so the check is repeated here.
  const next = safeNext(search.next);
  const router = useRouter();
  const [hint, setHint] = useState(readHint);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(
    search.error === 'no_sign_in' ? NO_SIGN_IN_MESSAGE : null,
  );

  async function signIn(event: FormEvent) {
    event.preventDefault();
    const value = hint.trim().toLowerCase();
    setHint(value);
    if (!isSignInHint(value)) {
      setError('That is not an organisation id or invite code.');
      return;
    }
    setBusy(true);
    setError(null);
    keepHint(value);
    try {
      await oauth.signIn(next ?? '/', value);
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
        <h1>Sign in to Delimitus</h1>
        {error ? (
          <div className="notice notice-danger" role="alert">
            <p>{error}</p>
          </div>
        ) : null}
        <form className="stack sign-in" noValidate onSubmit={(e) => void signIn(e)}>
          <label className="field">
            <span>Organisation id or invite code</span>
            <input
              name="hint"
              value={hint}
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              aria-describedby="sign-in-hint"
              onChange={(e) => setHint(e.target.value)}
            />
          </label>
          <p id="sign-in-hint" className="muted">
            Your admin gives you the organisation id. Pilot users enter their invite code.
          </p>
          <Button variant="primary" className="btn-lg" type="submit" disabled={busy}>
            Continue
          </Button>
        </form>
        {DevLogin ? (
          <Suspense fallback={<p className="muted">Loading…</p>}>
            <DevLogin api={api} session={session} onSignedIn={() => router.history.push(next ?? '/')} />
          </Suspense>
        ) : null}
      </div>
    </main>
  );
}
