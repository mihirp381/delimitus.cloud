import type { QueryClient } from '@tanstack/react-query';
import type { ApiClient, ApiQueries } from './api/client';
import type { Whoami } from './auth/admin';
import type { Session } from './auth/session';

/** What every route can reach through `Route.useRouteContext()`. */
export interface RouterContext {
  readonly api: ApiClient;
  readonly queries: ApiQueries;
  readonly session: Session;
  readonly queryClient: QueryClient;
  /** Whether to show admin-only screens; `auth/admin.ts` unless a test replaces it. */
  readonly isAdmin: (me: Whoami | undefined) => boolean;
}
