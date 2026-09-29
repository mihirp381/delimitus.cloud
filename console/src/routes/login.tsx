import { createFileRoute, useRouter } from '@tanstack/react-router';
import { lazy, Suspense } from 'react';

// The same test as DEV_LOGIN in ../auth/flags, written out so the bundler sees a literal `false`
// in a production build without VITE_SSC_DEV_LOGIN=1 and never emits the DevLogin chunk. An
// imported constant is folded too late: the chunk would still be written.
const DevLogin =
  import.meta.env.DEV || __SSC_DEV_LOGIN__ ? lazy(() => import('../auth/DevLogin')) : null;

/** Only same-site paths are followed after sign-in. */
function safeNext(value: unknown): string | undefined {
  return typeof value === 'string' && value.startsWith('/') && !value.startsWith('//')
    ? value
    : undefined;
}

export const Route = createFileRoute('/login')({
  validateSearch: (search: Record<string, unknown>): { next?: string } => {
    const next = safeNext(search.next);
    return next ? { next } : {};
  },
  component: LoginPage,
});

function LoginPage() {
  const { api, session } = Route.useRouteContext();
  // The router passes unvalidated search keys through, so the check is repeated here.
  const next = safeNext(Route.useSearch().next);
  const router = useRouter();
  return (
    <main className="login">
      <div className="panel stack">
        <h1>Sign in to SSC</h1>
        {DevLogin ? (
          <Suspense fallback={<p className="muted">Loading…</p>}>
            <DevLogin api={api} session={session} onSignedIn={() => router.history.push(next ?? '/')} />
          </Suspense>
        ) : (
          <p>Sign-in through the SSC auth host is not available yet.</p>
        )}
      </div>
    </main>
  );
}
