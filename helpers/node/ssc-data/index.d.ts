export const SERVICE: 'ssc-datagw';
export const METADATA: 'http://metadata.google.internal';
export const REGION_PATH: '/computeMetadata/v1/instance/region';
export const IDENTITY_PATH: '/computeMetadata/v1/instance/service-accounts/default/identity';
export const URL_VARIABLE: 'SSC_DATAGW_URL';
export const IDENTITY_HEADER: 'x-ssc-identity';

/** One result column; `type` is portable, `db_type` is the database's own. */
export interface Column {
  name: string;
  type: string;
  db_type: string;
}

/** A decimal is a string, a timestamp is ISO 8601, bytes are base64. */
export type Value = string | number | boolean | null | Value[] | { [key: string]: Value };

export interface QueryResult {
  columns: Column[];
  rows: Value[][];
  rowCount: number;
  truncated: boolean;
  truncatedReason: 'max_rows' | 'max_bytes' | 'daily_rows' | 'daily_bytes' | null;
  requestId: string;
}

export interface QueryOptions {
  maxRows?: number;
  maxBytes?: number;
  timeoutMs?: number;
  /** The `X-SSC-Identity` value of the request being answered. */
  identity?: string;
}

export type Param = string | number | boolean | null;

/** A refusal (the data gateway's code) or `UNREACHABLE`; `sqlstate` for `QUERY_FAILED`. */
export class DataError extends Error {
  readonly code: string;
  readonly status: number | null;
  readonly sqlstate: string | null;
  constructor(code: string, message: string, status?: number | null, sqlstate?: string | null);
}

/** `https://ssc-datagw-<project number>.<region>.run.app` of the cell this app runs in. */
export function gatewayUrl(options?: { metadata?: string }): Promise<string>;

/** The data gateway of the cell this app runs in; `url` and `metadata` are for tests. */
export class Data {
  constructor(options?: { url?: string; metadata?: string });
  query(name: string, sql: string, params?: Param[], options?: QueryOptions): Promise<QueryResult>;
}

export function query(name: string, sql: string, params?: Param[], options?: QueryOptions): Promise<QueryResult>;
