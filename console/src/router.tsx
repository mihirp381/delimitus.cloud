import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createRouter, RouterProvider, type RouterHistory } from '@tanstack/react-router';
import { createApiClient, createQueries } from './api/client';
import { ApiProblem } from './api/problem';
import { isAdmin as defaultIsAdmin } from './auth/admin';
import type { Session } from './auth/session';
import type { RouterContext } from './context';
import { routeTree } from './routeTree.gen';

interface Options {
  readonly baseUrl: string;
  readonly session: Session;
  readonly fetch?: (request: Request) => Promise<Response>;
  readonly history?: RouterHistory;
  readonly isAdmin?: RouterContext['isAdmin'];
}

function newQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // A refusal is an answer, not a blip: retry only network errors and 5xx.
        retry: (count, error) =>
          count < 2 && !(error instanceof ApiProblem && error.status < 500),
        refetchOnWindowFocus: false,
      },
    },
  });
}

export function createConsole({ baseUrl, session, fetch, history, isAdmin }: Options) {
  const queryClient = newQueryClient();
  const api = createApiClient({
    baseUrl,
    fetch,
    getToken: session.token,
    onUnauthorized: () => {
      session.clear();
      queryClient.clear();
      const { pathname, href } = router.state.location;
      if (pathname !== '/login') void router.navigate({ to: '/login', search: { next: href } });
    },
  });
  const context: RouterContext = {
    api,
    queries: createQueries(api),
    session,
    queryClient,
    isAdmin: isAdmin ?? defaultIsAdmin,
  };
  const router = createRouter({ routeTree, context, history, defaultPreload: false });
  return { router, queryClient, context };
}

export type ConsoleRouter = ReturnType<typeof createConsole>['router'];

declare module '@tanstack/react-router' {
  interface Register {
    router: ConsoleRouter;
  }
}

export function App({ console }: { readonly console: ReturnType<typeof createConsole> }) {
  return (
    <QueryClientProvider client={console.queryClient}>
      <RouterProvider router={console.router} />
    </QueryClientProvider>
  );
}
