export const SERVICE: 'ssc-datagw';
export const METADATA: 'http://metadata.google.internal';
export const REGION_PATH: '/computeMetadata/v1/instance/region';
export const IDENTITY_PATH: '/computeMetadata/v1/instance/service-accounts/default/identity';
export const URL_VARIABLE: 'SSC_DATAGW_URL';
export const DEFAULT_CONTENT_TYPE: 'application/octet-stream';

/** A signed link: send `headers` with `method` to `url` before `expires_at`. */
export interface Link {
  url: string;
  method: 'PUT' | 'GET';
  headers: Record<string, string>;
  expires_at: string;
  max_bytes?: number;
  request_id: string;
}

/** A refusal (the data gateway's code), `STORAGE_<status>`, or `UNREACHABLE`. */
export class FilesError extends Error {
  readonly code: string;
  readonly status: number | null;
  constructor(code: string, message: string, status?: number | null);
}

export type Body = Uint8Array | ArrayBuffer | Blob | string;

/** `https://ssc-datagw-<project number>.<region>.run.app` of the cell this app runs in. */
export function gatewayUrl(options?: { metadata?: string }): Promise<string>;

/** The file broker of the cell this app runs in; `url` and `metadata` are for tests. */
export class Files {
  constructor(options?: { url?: string; metadata?: string });
  put(name: string, data: Body, options?: { contentType?: string }): Promise<void>;
  get(name: string): Promise<Uint8Array>;
  remove(name: string): Promise<void>;
  link(op: 'put' | 'get', name: string, options?: { contentType?: string }): Promise<Link>;
}

export function put(name: string, data: Body, options?: { contentType?: string }): Promise<void>;
export function get(name: string): Promise<Uint8Array>;
export function remove(name: string): Promise<void>;
export function link(op: 'put' | 'get', name: string, options?: { contentType?: string }): Promise<Link>;
