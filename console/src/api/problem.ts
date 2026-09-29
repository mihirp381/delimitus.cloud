import type { components } from './schema';

export type Problem = components['schemas']['Problem'];
export type ErrorCode = components['schemas']['ErrorCode'];

export const PROBLEM_MEDIA_TYPE = 'application/problem+json';

function isProblem(body: unknown): body is Problem {
  if (typeof body !== 'object' || body === null) return false;
  const b = body as Record<string, unknown>;
  return typeof b.code === 'string' && typeof b.title === 'string' && typeof b.status === 'number';
}

/**
 * Every refusal from the API, as one error type. `code` is null when the body is not an SSC
 * problem (for example a proxy error page); the UI then shows the HTTP status.
 */
export class ApiProblem extends Error {
  readonly status: number;
  readonly code: ErrorCode | null;
  readonly title: string;
  readonly detail: string;
  readonly requestId: string | null;

  constructor(status: number, problem: Problem | null, statusText = '') {
    const title = problem?.title ?? (statusText || `HTTP ${status}`);
    super(problem ? `${problem.code}: ${problem.title}` : title);
    this.name = 'ApiProblem';
    this.status = status;
    this.code = problem?.code ?? null;
    this.title = title;
    this.detail = problem?.detail ?? '';
    this.requestId = problem?.request_id ?? null;
  }
}

/** Reads a non-2xx response. The body is parsed as a problem only when it declares JSON. */
export async function toProblem(response: Response): Promise<ApiProblem> {
  const type = response.headers.get('Content-Type') ?? '';
  let body: unknown = null;
  if (type.startsWith(PROBLEM_MEDIA_TYPE) || type.startsWith('application/json')) {
    try {
      body = await response.json();
    } catch {
      body = null;
    }
  }
  return new ApiProblem(response.status, isProblem(body) ? body : null, response.statusText);
}
