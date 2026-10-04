/**
 * Read a database the org connected, through the cell's data gateway (SSC-052). Name the
 * connection in `[connections] names` in `ssc.toml`; an admin grants the environment the
 * connection.
 *
 *   import { query } from '@delimitus/ssc-data';
 *
 *   const result = await query('finance', 'select id, total from invoices where year = $1', [2026]);
 *   for (const row of result.rows) { ... }   // each row is an array in column order
 *
 * The call carries the app's own workload token, so the app holds no database credentials. The
 * statement is one read-only `SELECT` with `$1`, `$2`, ... placeholders; `maxRows`, `maxBytes` and
 * `timeoutMs` only narrow what the platform, the connection and the grant allow, and `truncated`
 * says the gateway stopped reading before the result ended. To act for the signed-in user, pass
 * the `X-SSC-Identity` value of the request being answered as `identity`; without it the app acts
 * for itself (docs/contracts/data-gateway.md).
 *
 * The data gateway's address comes from the metadata server: Cloud Run's `instance/region` names
 * the project number and the region, and the gateway is `ssc-datagw` there. `SSC_DATAGW_URL` or
 * `{ url }` replaces it. A query only reads, so it is tried once more when the data gateway cannot
 * be reached or answers 502, 503 or 504; a query that timed out is not. Same names and behaviour
 * as the Python helper `ssc_app.data`, where the asks are `max_rows`, `max_bytes`, `timeout_ms`.
 */

export const SERVICE = 'ssc-datagw';
export const METADATA = 'http://metadata.google.internal';
export const REGION_PATH = '/computeMetadata/v1/instance/region';
export const IDENTITY_PATH = '/computeMetadata/v1/instance/service-accounts/default/identity';
export const URL_VARIABLE = 'SSC_DATAGW_URL';
export const IDENTITY_HEADER = 'x-ssc-identity';
const METADATA_TIMEOUT_MS = 5000;
const QUERY_TIMEOUT_MS = 60000;
const REFRESH_BEFORE_S = 300;
const RETRY_STATUSES = new Set([502, 503, 504]);

/**
 * A refusal or a failure. `code` is the data gateway's (`CONNECTION_NOT_GRANTED`,
 * `CONNECTION_SUSPENDED`, `QUERY_REFUSED`, `QUERY_FAILED`, `DAILY_BUDGET_SPENT`, ...) or
 * `UNREACHABLE`; `sqlstate` is the database's for `QUERY_FAILED` when it gave one.
 */
export class DataError extends Error {
  constructor(code, message, status = null, sqlstate = null) {
    super(`${code}: ${message}`);
    this.name = 'DataError';
    this.code = code;
    this.status = status;
    this.sqlstate = sqlstate;
  }
}

async function metadataGet(metadata, path) {
  let response;
  try {
    response = await fetch(`${metadata}${path}`, {
      headers: { 'Metadata-Flavor': 'Google' },
      signal: AbortSignal.timeout(METADATA_TIMEOUT_MS),
    });
  } catch (err) {
    throw new DataError('UNREACHABLE', `metadata server: ${err.name}`);
  }
  if (!response.ok) throw new DataError('UNREACHABLE', `metadata server answered HTTP ${response.status}`);
  return (await response.text()).trim();
}

/** `https://ssc-datagw-<project number>.<region>.run.app` of the cell this app runs in. */
export async function gatewayUrl({ metadata = METADATA } = {}) {
  const parts = (await metadataGet(metadata, REGION_PATH)).split('/');
  if (parts.length !== 4 || parts[0] !== 'projects' || parts[2] !== 'regions') {
    throw new DataError('UNREACHABLE', 'the metadata server did not name a region');
  }
  return `https://${SERVICE}-${parts[1]}.${parts[3]}.run.app`;
}

function expiry(token) {
  try {
    const claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString());
    if (typeof claims.exp !== 'number') throw new Error('no exp');
    return claims.exp;
  } catch {
    throw new DataError('UNREACHABLE', 'the metadata server returned an unreadable ID token');
  }
}

async function refusal(response) {
  try {
    const { error } = await response.json();
    return new DataError(String(error.code), String(error.message), response.status, error.sqlstate ?? null);
  } catch {
    return new DataError('UNAVAILABLE', `the data gateway answered HTTP ${response.status}`, response.status);
  }
}

/**
 * The data gateway of the cell this app runs in. `url` and `metadata` replace the data gateway's
 * address and the metadata server's (tests).
 */
export class Data {
  constructor({ url, metadata = METADATA } = {}) {
    const given = (url || process.env[URL_VARIABLE] || '').replace(/\/+$/, '');
    this._url = given ? Promise.resolve(given) : null;
    this._metadata = metadata;
    this._token = null;
  }

  /**
   * Run one read-only statement on connection `name`: `{ columns, rows, rowCount, truncated,
   * truncatedReason, requestId }`. `DataError` for a refusal.
   */
  async query(name, sql, params = [], { maxRows, maxBytes, timeoutMs, identity } = {}) {
    const body = { sql, params };
    if (maxRows !== undefined) body.max_rows = maxRows;
    if (maxBytes !== undefined) body.max_bytes = maxBytes;
    if (timeoutMs !== undefined) body.timeout_ms = timeoutMs;
    const url = await this._gateway();
    for (const attempt of [1, 2]) {
      const headers = { authorization: await this._bearer(url), 'content-type': 'application/json' };
      if (identity !== undefined) headers[IDENTITY_HEADER] = identity;
      let response;
      try {
        response = await fetch(`${url}/v1/connections/${name}/query`, {
          method: 'POST',
          headers,
          body: JSON.stringify(body),
          signal: AbortSignal.timeout(QUERY_TIMEOUT_MS),
        });
      } catch (err) {
        if (err instanceof DataError) throw err;
        if (err.name === 'TimeoutError') throw new DataError('UNREACHABLE', 'data gateway: TimeoutError');
        if (attempt === 1) continue;
        throw new DataError('UNREACHABLE', `data gateway: ${err.name}`);
      }
      if (RETRY_STATUSES.has(response.status) && attempt === 1) {
        await response.body?.cancel();
        continue;
      }
      if (response.status !== 200) throw await refusal(response);
      const answer = await response.json();
      return {
        columns: answer.columns,
        rows: answer.rows,
        rowCount: answer.row_count,
        truncated: answer.truncated,
        truncatedReason: answer.truncated_reason,
        requestId: answer.request_id,
      };
    }
    throw new Error('unreachable');
  }

  async _gateway() {
    this._url ??= gatewayUrl({ metadata: this._metadata }).catch((err) => {
      this._url = null;
      throw err;
    });
    return this._url;
  }

  async _bearer(audience) {
    const now = Date.now() / 1000;
    if (this._token === null || this._token.exp - REFRESH_BEFORE_S <= now) {
      const query = new URLSearchParams({ audience, format: 'full' });
      const token = await metadataGet(this._metadata, `${IDENTITY_PATH}?${query}`);
      this._token = { token, exp: expiry(token) };
    }
    return `Bearer ${this._token.token}`;
  }
}

let shared = null;

/** `Data#query` on this app's cell. */
export function query(name, sql, params, options) {
  shared ??= new Data();
  return shared.query(name, sql, params, options);
}
