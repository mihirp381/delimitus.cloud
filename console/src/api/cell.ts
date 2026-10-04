import type { components } from './schema';

export type CellOut = components['schemas']['CellOut'];
export type CellResource = components['schemas']['CellResourceOut'];
export type CellEnvironment = components['schemas']['CellEnvironmentOut'];
export type CellDatabase = components['schemas']['CellDatabaseOut'];
export type Usage = components['schemas']['UsageOut'];
export type Health = components['schemas']['HealthOut'];
export type Database = components['schemas']['DatabaseOut'];

export const RESOURCE_TITLE: Readonly<Record<CellResource['resource'], string>> = {
  database: 'Database',
  egress: 'Egress proxy',
  connections: 'Data gateway',
};

export const CAUSE_TEXT: Readonly<Record<NonNullable<CellResource['cause']>, string>> = {
  deploy: 'A deploy that needed a database',
  egress_approved: 'An allowed internet host',
  connection_granted: 'A data connection',
  file_use: 'File use',
  admin: 'An org admin',
};

export const BILLING_TEXT: Readonly<Record<NonNullable<Usage['billing']>, string>> = {
  request: 'Request-billed',
  instance: 'Instance-billed',
};

/** "about $13 a month": the figures are what a choice sets off, never a bill (A6). */
export function monthly(usd: number): string {
  return usd === 0 ? 'nothing more' : `about $${usd} a month`;
}

export function hours(value: number): string {
  return `${value.toFixed(value < 10 ? 2 : 1)} h`;
}

export function when(value: string): string {
  return new Date(value).toLocaleString();
}

/** How often an app started from zero this month and how long it took. */
export function coldStarts(u: Usage): string {
  if (u.cold_starts === 0) return 'None';
  if (u.cold_start_p50_seconds === null || u.cold_start_p95_seconds === null) {
    return `${u.cold_starts}, too few for timings`;
  }
  return `${u.cold_starts}, median ${u.cold_start_p50_seconds}s, slowest 5% ${u.cold_start_p95_seconds}s`;
}

/** Bytes as the console shows them: B, KB, MB or GB, with one decimal past bytes. */
export function size(bytes: number): string {
  const units = ['B', 'KB', 'MB', 'GB'] as const;
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${value} B` : `${value.toFixed(1)} ${units[unit]}`;
}
