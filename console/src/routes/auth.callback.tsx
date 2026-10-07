import { createFileRoute, Link, useRouter } from '@tanstack/react-router';
import { useEffect, useRef, useState } from 'react';
import { safeNext, signInMessage } from '../auth/oauth';

/**
 * Where the auth host sends the person back (decision 029): `?code&state&iss`, or `?error`. The
 * answer is checked and exchanged once; the tokens go to the in-memory session and the history
 * entry holding the code is replaced by the page the person asked for.
 */
export const Route = createFileRoute('/auth/callback')({
  component: Callback,
});

function Callback() {
  const { oauth, session, queryClient } = Route.useRouteContext();
  const router = useRouter();
  // The raw query, before the router parses it; captured once, as the answer is good once.
  const search = useRef(router.history.location.search);
  const started = useRef(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    oauth.complete(search.current).then(
      ({ tokens, next }) => {
        queryClient.clear();
        session.start(tokens);
        router.history.replace(safeNext(next) ?? '/');
      },
      (e: unknown) => setError(signInMessage(e)),
    );
  }, [oauth, session, queryClient, router]);

  return (
    <main className="login">
      <div className="panel stack">
        <h1>Sign in to SSC</h1>
        {error ? (
          <>
            <div className="notice notice-danger" role="alert">
              <p>{error}</p>
            </div>
            <p>
              <Link to="/login">Try again</Link>
            </p>
          </>
        ) : (
          <p className="muted">Finishing sign-in…</p>
        )}
      </div>
    </main>
  );
}
