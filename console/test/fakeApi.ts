/** A fetch that answers from a table of routes and records every request it sees. */

export interface Call {
  readonly method: string;
  readonly path: string;
  readonly headers: Headers;
  readonly body: unknown;
}

export type Handler = (call: Call) => Response | Promise<Response>;

export const ORIGIN = 'http://console.test';

export function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

export function problem(status: number, code: string, title: string, detail = ''): Response {
  return new Response(
    JSON.stringify({
      type: `https://errors.ssc.invalid/${code}`,
      title,
      status,
      detail: detail || title,
      instance: '/v1/test',
      code,
      request_id: 'req_test_0001',
    }),
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  );
}

/**
 * `routes` maps "METHOD /path" to a handler, or to a list of handlers used one per call (the last
 * one repeats). An unknown route answers 404 NOT_FOUND.
 */
export function fakeApi(routes: Record<string, Handler | Handler[]>) {
  const calls: Call[] = [];
  const used = new Map<string, number>();
  async function fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const text = await request.text();
    const call: Call = {
      method: request.method,
      path: url.pathname,
      headers: request.headers,
      body: text ? JSON.parse(text) : undefined,
    };
    calls.push(call);
    const key = `${call.method} ${call.path}`;
    const entry = routes[key];
    if (!entry) return problem(404, 'NOT_FOUND', 'Not found');
    const list = Array.isArray(entry) ? entry : [entry];
    const n = used.get(key) ?? 0;
    used.set(key, n + 1);
    const handler = list[Math.min(n, list.length - 1)];
    if (!handler) throw new Error(`no handler for ${key}`);
    return handler(call);
  }
  return { fetch, calls, of: (method: string, path: string) => calls.filter((c) => c.method === method && c.path === path) };
}
