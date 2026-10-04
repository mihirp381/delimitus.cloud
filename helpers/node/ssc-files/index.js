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
 * it cannot be reached, times out, or answers 502, 503 or 504. Same names and behaviour as the
 * Python helper `ssc_app.files`, where `remove` is `delete`.
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
 * the transfer, or `UNREACHABLE`.
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

async function refusal(response) {
  try {
    const { error } = await response.json();
    return new FilesError(String(error.code), String(error.message), response.status);
  } catch {
    return new FilesError('UNAVAILABLE', `the data gateway answered HTTP ${response.status}`, response.status);
  }
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

  /** Store `data` as `name`, replacing a file of that name. */
  async put(name, data, { contentType = DEFAULT_CONTENT_TYPE } = {}) {
    await this._transfer(await this.link('put', name, { contentType }), data);
  }

  /** The bytes of `name`; `FilesError` `FILE_NOT_FOUND` when there is none. */
  async get(name) {
    return this._transfer(await this.link('get', name), undefined);
  }

  /** Remove `name`; `FilesError` `FILE_NOT_FOUND` when there is none. */
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
      let response;
      try {
        response = await fetch(`${url}/v1/files/${op}`, {
          method: 'POST',
          headers: { authorization: await this._bearer(url), 'content-type': 'application/json' },
          body: JSON.stringify(body),
          signal: AbortSignal.timeout(GATEWAY_TIMEOUT_MS),
        });
      } catch (err) {
        if (err instanceof FilesError) throw err;
        if (attempt === 1) continue;
        throw new FilesError('UNREACHABLE', `data gateway: ${err.name}`);
      }
      if (RETRY_STATUSES.has(response.status) && attempt === 1) {
        await response.body?.cancel();
        continue;
      }
      if (response.status !== 200) throw await refusal(response);
      return response.json();
    }
    throw new Error('unreachable');
  }

  async _transfer(link, data) {
    let response;
    try {
      response = await fetch(link.url, {
        method: link.method,
        headers: link.headers,
        body: data,
        signal: AbortSignal.timeout(TRANSFER_TIMEOUT_MS),
      });
    } catch (err) {
      throw new FilesError('UNREACHABLE', `Cloud Storage: ${err.name}`);
    }
    if (response.status === 404 && link.method === 'GET') {
      throw new FilesError('FILE_NOT_FOUND', 'the file is gone', 404);
    }
    if (!response.ok) {
      await response.body?.cancel();
      throw new FilesError(`STORAGE_${response.status}`, 'Cloud Storage refused the transfer', response.status);
    }
    return new Uint8Array(await response.arrayBuffer());
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
