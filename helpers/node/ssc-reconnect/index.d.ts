export const DEADLINE_HEADER: 'X-SSC-Request-Deadline';
export const RESTART_CODE: 1012;
export const DEFAULT_MARGIN_SECONDS: 30;
export const DEFAULT_RETRY_MS: 1000;

export type RequestHeaders = Headers | Record<string, string | string[] | undefined> | null | undefined;

/** Seconds until the gateway's limit ends this request, never below 0; null without the header. */
export function secondsLeft(headers: RequestHeaders, options?: { now?: number }): number | null;

/** Seconds to keep a stream: `left - margin`, or `left / 2` when later; null for no deadline or one passed. */
export function secondsToWait(left: number | null | undefined, margin?: number): number | null;

/** Ends `res` `margin` seconds before the deadline with a `retry:` hint. Returns a cancel function. */
export function endBeforeDeadline(
  res: { write(chunk: string): unknown; end(): unknown },
  headers: RequestHeaders,
  options?: { margin?: number; retryMs?: number },
): () => void;

/** Closes `socket` with `RESTART_CODE` `margin` seconds before the deadline. Returns a cancel function. */
export function closeBeforeDeadline(
  socket: { close(code: number, reason: string): unknown },
  headers: RequestHeaders,
  options?: { margin?: number },
): () => void;

/** The dependency-free browser client that defines `sscSocket(url, options)`. */
export function browserClient(): string;
