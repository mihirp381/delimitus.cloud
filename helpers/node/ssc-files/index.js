/**
 * Keep files through the cell's file broker (SSC-046). Ask for it with `[files]` in `ssc.toml`.
 *
 *   import { put, get, link, remove } from '@delimitus/ssc-files';
 *
 *   await put('photos/cat.png', bytes, { contentType: 'image/png' });
 *   const data = await get('photos/cat.png');               // a Uint8Array
 *   const { url } = await link('get', 'photos/cat.png');    // send a browser there to download
 *   await remove('photos/cat.png');
 *
 * Each call asks the data gateway for a signed link with the app's own workload token, then sends
 * the bytes to Cloud Storage with it; the app holds no storage credentials. A file is at most
 * 25 MB, a name is `/`-separated segments of letters, digits, `.`, `_` and `-`, and a download
 * always arrives as an attachment, never rendered as a page (docs/contracts/data-gateway.md).
 *
 * The data gateway's address comes from the metadata server: Cloud Run's `instance/region` names
 * the project number and the region, and the gateway is `ssc-datagw` there. `SSC_DATAGW_URL` or
 * `{ url }` replaces it. The data gateway runs from zero, so a call to it is tried once more when
 * it cannot be reached, times out, or answers 502, 503 or 504. Remove is idempotent: a retried
 * remove that finds nothing succeeds. Same names and behaviour as the Python helper
 * `ssc_app.files`, where `remove` is `delete`.
 */

export const SERVICE = 'ssc-datagw';
export const METADATA = 'http://metadata.google.internal';
export const REGION_PATH = '/computeMetadata/v1/instance/region';
export const IDENTITY_PATH = '/computeMetadata/v1/instance/service-accounts/default/identity';
export const URL_VARIABLE = 'SSC_DATAGW_URL';
export const DEFAULT_CONTENT_TYPE = 'application/octet-stream';
const METADATA_TIMEOUT_MS = 5000;
const GATEWAY_TIMEOUT_MS = 30000;
const TRANSFER_TIMEOUT_MS = 120000;
const REFRESH_BEFORE_S = 300;
const RETRY_STATUSES = new Set([502, 503, 504]);

/**
 * A refusal or a failure. `code` is the data gateway's (`FILE_NOT_FOUND`,
 * `FILES_QUOTA_EXCEEDED`, `APP_NOT_ACTIVE`, ...), `STORAGE_<status>` when Cloud Storage refused
 * the transfer, `FILE_TOO_LARGE` (over the link's `max_bytes`, nothing sent), `UNREACHABLE`
 * (the gateway, Cloud Storage or the workload token could not be had) or `BAD_GATEWAY_RESPONSE`
 * (an answer that is not JSON).
 */
export class FilesError extends Error {
  constructor(code, message, status = null) {
    super(`${code}: ${message}`);
    this.name = 'FilesError';
    this.code = code;
    this.status = status;
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
    throw new FilesError('UNREACHABLE', `metadata server: ${err.name}`);
  }
  if (!response.ok) throw new FilesError('UNREACHABLE', `metadata server answered HTTP ${response.status}`);
  return (await response.text()).trim();
}

/** `https://ssc-datagw-<project number>.<region>.run.app` of the cell this app runs in. */
export async function gatewayUrl({ metadata = METADATA } = {}) {
  const parts = (await metadataGet(metadata, REGION_PATH)).split('/');
  if (parts.length !== 4 || parts[0] !== 'projects' || parts[2] !== 'regions') {
    throw new FilesError('UNREACHABLE', 'the metadata server did not name a region');
  }
  return `https://${SERVICE}-${parts[1]}.${parts[3]}.run.app`;
}

function expiry(token) {
  try {
    const claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString());
    if (typeof claims.exp !== 'number') throw new Error('no exp');
    return claims.exp;
  } catch {
    throw new FilesError('UNREACHABLE', 'the metadata server returned an unreadable ID token');
  }
}

/** The size of a body `put` takes, in bytes; null for anything else, which `fetch` judges. */
function byteLength(data) {
  if (typeof data === 'string') return Buffer.byteLength(data);
  if (data instanceof ArrayBuffer || ArrayBuffer.isView(data)) return data.byteLength;
  if (typeof Blob !== 'undefined' && data instanceof Blob) return data.size;
  return null;
}

function refusal(status, raw) {
  try {
    const { error } = JSON.parse(raw);
    return new FilesError(String(error.code), String(error.message), status);
  } catch {
    return new FilesError('BAD_GATEWAY_RESPONSE', `the data gateway answered HTTP ${status}`, status);
  }
}

function answerOf(status, raw) {
  let answer;
  try {
    answer = JSON.parse(raw);
  } catch {
    answer = null;
  }
  if (answer === null || typeof answer !== 'object' || Array.isArray(answer)) {
    throw new FilesError('BAD_GATEWAY_RESPONSE', "the data gateway's answer is not JSON", status);
  }
  return answer;
}

/**
 * The file broker of the cell this app runs in. `url` and `metadata` replace the data gateway's
 * address and the metadata server's (tests).
 */
export class Files {
  constructor({ url, metadata = METADATA } = {}) {
    const given = (url || process.env[URL_VARIABLE] || '').replace(/\/+$/, '');
    this._url = given ? Promise.resolve(given) : null;
    this._metadata = metadata;
    this._token = null;
  }

  /**
   * Store `data` as `name`, replacing a file of that name. `FilesError` `FILE_TOO_LARGE`, before
   * anything is sent, when `data` is over the link's `max_bytes`.
   */
  async put(name, data, { contentType = DEFAULT_CONTENT_TYPE } = {}) {
    const link = await this.link('put', name, { contentType });
    const size = byteLength(data);
    if (Number.isInteger(link.max_bytes) && size !== null && size > link.max_bytes) {
      throw new FilesError('FILE_TOO_LARGE', `${size} bytes is over the ${link.max_bytes} byte limit`);
    }
    await this._transfer(link, data);
  }

  /** The bytes of `name`; `FilesError` `FILE_NOT_FOUND` when there is none. */
  async get(name) {
    return this._transfer(await this.link('get', name), undefined);
  }

  /**
   * Remove `name`; `FilesError` `FILE_NOT_FOUND` when there is none. Remove is idempotent: a
   * retried remove that finds nothing succeeds.
   */
  async remove(name) {
    await this._ask('delete', { name });
  }

  /**
   * The signed link itself: `url`, `method`, `headers` to send, `expires_at` (10 minutes), and
   * `max_bytes` for `put`. A `get` link suits a browser redirect.
   */
  async link(op, name, { contentType } = {}) {
    const body = { name };
    if (contentType !== undefined) body.content_type = contentType;
    return this._ask(op, body);
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

  async _ask(op, body) {
    const url = await this._gateway();
    for (const attempt of [1, 2]) {
      const authorization = await this._bearer(url);
      let status;
      let raw;
      try {
        const response = await fetch(`${url}/v1/files/${op}`, {
          method: 'POST',
          headers: { authorization, 'content-type': 'application/json' },
          body: JSON.stringify(body),
          signal: AbortSignal.timeout(GATEWAY_TIMEOUT_MS),
        });
        status = response.status;
        raw = await response.text();
      } catch (err) {
        if (attempt === 1) continue;
        throw new FilesError('UNREACHABLE', `data gateway: ${err.name}`);
      }
      if (RETRY_STATUSES.has(status) && attempt === 1) continue;
      if (status !== 200) {
        const refused = refusal(status, raw);
        // The first delete went through and its answer was lost.
        if (op === 'delete' && attempt === 2 && refused.code === 'FILE_NOT_FOUND') return {};
        throw refused;
      }
      return answerOf(status, raw);
    }
    throw new Error('unreachable');
  }

  async _transfer(link, data) {
    let status;
    let bytes;
    try {
      const response = await fetch(link.url, {
        method: link.method,
        headers: link.headers,
        body: data,
        signal: AbortSignal.timeout(TRANSFER_TIMEOUT_MS),
      });
      status = response.status;
      bytes = new Uint8Array(await response.arrayBuffer());
    } catch (err) {
      throw new FilesError('UNREACHABLE', `Cloud Storage: ${err.name}`);
    }
    if (status === 404 && link.method === 'GET') {
      throw new FilesError('FILE_NOT_FOUND', 'the file is gone', 404);
    }
    if (status < 200 || status >= 300) {
      throw new FilesError(`STORAGE_${status}`, 'Cloud Storage refused the transfer', status);
    }
    return bytes;
  }
}

let shared = null;

function files() {
  shared ??= new Files();
  return shared;
}

/** `Files#put` on this app's cell. */
export function put(name, data, options) {
  return files().put(name, data, options);
}

/** `Files#get` on this app's cell. */
export function get(name) {
  return files().get(name);
}

/** `Files#remove` on this app's cell. */
export function remove(name) {
  return files().remove(name);
}

/** `Files#link` on this app's cell. */
export function link(op, name, options) {
  return files().link(op, name, options);
}
