export const IDENTITY_HEADER: 'X-SSC-Identity';
export const IDENTITY_TYP: 'ssc-id+jwt';
export const IDENTITY_ALG: 'ES256';
export const MAX_TTL_SECONDS: 300;
export const MAX_GROUPS: 50;
export const DEFAULT_LEEWAY_SECONDS: 30;

export type RefusalCode =
  | 'missing' | 'malformed' | 'wrong_type' | 'wrong_algorithm' | 'unknown_key' | 'bad_signature'
  | 'wrong_audience' | 'wrong_issuer' | 'expired' | 'not_yet_valid' | 'ttl_too_long' | 'bad_claims';
export const REFUSAL_CODES: readonly RefusalCode[];

export class IdentityRefused extends Error {
  readonly code: RefusalCode;
  constructor(code: RefusalCode, message: string);
}

export interface Jwk { kty: string; crv?: string; x?: string; y?: string; kid?: string; use?: string; alg?: string }
export interface Jwks { keys: Jwk[] }

/** The verified claims. Key on `sub`; `name` and `email` are display only and null for schedules. */
export interface IdentityNote {
  readonly iss: string;
  readonly aud: string;
  readonly sub: string;
  readonly iat: number;
  readonly exp: number;
  readonly org: string;
  readonly app: string;
  readonly env: 'prod' | 'preview';
  readonly role: 'builder' | 'user' | 'schedule';
  readonly groups: readonly string[];
  readonly name: string | null;
  readonly email: string | null;
  readonly isSchedule: boolean;
}

export interface VerifyOptions {
  /** This app's exact origin, `https://host`, no path. */
  audience: string;
  /** The cell JWKS, or its URL (`<issuer>/jwks.json`). */
  keys: Jwks | string;
  issuer?: string;
  /** Unix seconds; for tests. */
  now?: number;
  leeway?: number;
}

export function verify(token: string | null | undefined, options: VerifyOptions): Promise<IdentityNote>;
export function checkClaims(claims: Record<string, unknown>, options: Omit<VerifyOptions, 'keys'>): IdentityNote;
export function validateNote(claims: Record<string, unknown>): IdentityNote;
export function tokenFromHeaders(headers: Headers | Record<string, string | string[] | undefined> | null | undefined): string | null;

export class IdentityVerifier {
  readonly audience: string;
  readonly issuer: string | undefined;
  readonly leeway: number;
  constructor(options: { audience: string; keys: Jwks | string; issuer?: string; leeway?: number });
  verify(token: string | null | undefined, options?: { now?: number }): Promise<IdentityNote>;
  fromHeaders(headers: Headers | Record<string, string | string[] | undefined> | null | undefined, options?: { now?: number }): Promise<IdentityNote>;
}
