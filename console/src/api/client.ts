import createClient, { type Middleware } from 'openapi-fetch';
import createQueryClient from 'openapi-react-query';
import { toProblem } from './problem';
import type { paths } from './schema';

export interface ApiOptions {
  /** An absolute origin; the console passes its own, which the proxy forwards to the API. */
  readonly baseUrl: string;
  readonly getToken: () => string | null;
  /** Called on any 401, before the problem is thrown. */
  readonly onUnauthorized?: () => void;
  readonly fetch?: (request: Request) => Promise<Response>;
  readonly newIdempotencyKey?: () => string;
}

/** Methods the API treats as non-idempotent; each call gets its own Idempotency-Key. */
const KEYED_METHODS = new Set(['POST', 'PATCH', 'DELETE']);

/**
 * The generated client with the console's rules: a Bearer token on every call, an
 * Idempotency-Key per mutation, and every non-2xx response thrown as an `ApiProblem`. A PUT is
 * guarded by If-Match instead; see `updateGrants` for the 412 retry.
 */
export function createApiClient(options: ApiOptions) {
  const newKey = options.newIdempotencyKey ?? (() => crypto.randomUUID());
  const client = createClient<paths>({
    baseUrl: options.baseUrl,
    fetch: options.fetch ?? ((request) => globalThis.fetch(request)),
  });
  const rules: Middleware = {
    onRequest({ request }) {
      const token = options.getToken();
      if (token && !request.headers.has('Authorization')) {
        request.headers.set('Authorization', `Bearer ${token}`);
      }
      if (KEYED_METHODS.has(request.method) && !request.headers.has('Idempotency-Key')) {
        request.headers.set('Idempotency-Key', newKey());
      }
      return request;
    },
    async onResponse({ response }) {
      if (response.ok) return undefined;
      const problem = await toProblem(response);
      if (problem.status === 401) options.onUnauthorized?.();
      throw problem;
    },
  };
  client.use(rules);
  return client;
}

export type ApiClient = ReturnType<typeof createApiClient>;

export function createQueries(api: ApiClient) {
  return createQueryClient(api);
}

export type ApiQueries = ReturnType<typeof createQueries>;

/** The data of a call that did not throw. openapi-fetch still types it as optional. */
export function must<T>(result: { data?: T; response: Response }): T {
  if (result.data === undefined) {
    throw new Error(`empty response body (HTTP ${result.response.status})`);
  }
  return result.data;
}
