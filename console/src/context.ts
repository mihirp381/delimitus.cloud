import type { QueryClient } from '@tanstack/react-query';
import type { ApiClient, ApiQueries } from './api/client';
import type { Session } from './auth/session';

/** What every route can reach through `Route.useRouteContext()`. */
export interface RouterContext {
  readonly api: ApiClient;
  readonly queries: ApiQueries;
  readonly session: Session;
  readonly queryClient: QueryClient;
}
