/**
 * Read the identity note the SSC gateway attaches to every request as `X-SSC-Identity`.
 *
 *   import { IdentityVerifier } from '@delimitus/ssc-identity';
 *   const verifier = new IdentityVerifier({
 *     audience: 'https://quiet-river-7f3k.delimitusapps.com',
 *     keys: 'https://keys.delimitus.com/cell-01/jwks.json',
 *   });
 *   const note = await verifier.fromHeaders(req.headers);
 *   const who = note.sub;     // key on this
 *   const label = note.name;  // display only
 *
 * Two rules:
 *  - Key on `sub`, never on `email`. `sub` is a stable `usr_` or `sch_` id; `email` and `name`
 *    are display strings the company directory can change, and a schedule note has neither.
 *  - Verify once, when the request arrives. Do not re-verify inside a long WebSocket or event
 *    stream: the note expires after five minutes and ending open streams is the platform kill
 *    switch's job. Re-verifying mid-stream only breaks the stream.
 *
 * Every refusal is an `IdentityRefused` with a `code` from `REFUSAL_CODES`. Treat them all the
 * same way: the request is not from a signed-in user of this app.
 *
 * Contract: docs/contracts/identity-note.md in the SSC repository. Same codes and the same
 * check order as the Python helper `ssc_app.identity`; both run the shared vectors.
 */

import { webcrypto } from 'node:crypto';

const { subtle } = webcrypto;

export const IDENTITY_HEADER = 'X-SSC-Identity';
export const IDENTITY_TYP = 'ssc-id+jwt';
export const IDENTITY_ALG = 'ES256';
export const MAX_TTL_SECONDS = 300;
export const MAX_GROUPS = 50;
export const DEFAULT_LEEWAY_SECONDS = 30;

export const REFUSAL_CODES = Object.freeze([
  'missing',
  'malformed',
  'wrong_type',
  'wrong_algorithm',
  'unknown_key',
  'bad_signature',
  'wrong_audience',
  'wrong_issuer',
  'expired',
  'not_yet_valid',
  'ttl_too_long',
  'bad_claims',
]);

export class IdentityRefused extends Error {
  /** @param {string} code @param {string} message */
  constructor(code, message) {
    super(`${code}: ${message}`);
    this.name = 'IdentityRefused';
    this.code = code;
  }
}

const RE = {
  iss: /^https:\/\/[a-z0-9.-]+\/[a-z0-9-]+$/,
  aud: /^https:\/\/[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(:[0-9]{1,5})?$/,
  sub: /^(usr|sch)_[a-z0-9]{20}$/,
  org: /^org_[a-z0-9]{20}$/,
  app: /^app_[a-z0-9]{20}$/,
  grp: /^grp_[a-z0-9]{20}$/,
  email: /^[^@\s]+@[^@\s]+\.[^@\s]+$/,
};
const ENVS = new Set(['prod', 'preview']);
const ROLES = new Set(['builder', 'user', 'schedule']);
const CLAIMS = new Set(['iss', 'aud', 'sub', 'iat', 'exp', 'org', 'app', 'env', 'role', 'groups', 'name', 'email']);

function b64url(segment) {
  if (typeof segment !== 'string' || !/^[A-Za-z0-9_-]*$/.test(segment)) {
    throw new IdentityRefused('malformed', 'segment is not base64url');
  }
  return Buffer.from(segment, 'base64url');
}

function parseJson(bytes, what) {
  let value;
  try {
    value = JSON.parse(bytes.toString('utf8'));
  } catch {
    throw new IdentityRefused('malformed', `${what} is not JSON`);
  }
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new IdentityRefused('malformed', `${what} is not an object`);
  }
  return value;
}

function isInt(v) {
  return typeof v === 'number' && Number.isInteger(v);
}

/** Static JWKS, or a URL fetched and cached; refreshed once when a kid is unknown (rotation). */
class KeySet {
  #url;
  #keys = new Map();
  #fetchedAt = 0;
  #lifespanMs = 300_000;

  constructor(keys) {
    if (typeof keys === 'string') this.#url = keys;
    else this.#load(keys);
  }

  #load(jwks) {
    if (!jwks || !Array.isArray(jwks.keys)) throw new TypeError('keys must be a JWKS with a keys array');
    this.#keys = new Map();
    for (const jwk of jwks.keys) {
      if (typeof jwk.kid === 'string' && jwk.kty === 'EC' && jwk.crv === 'P-256') this.#keys.set(jwk.kid, jwk);
    }
    this.#fetchedAt = Date.now();
  }

  async #refresh() {
    const res = await fetch(this.#url, { headers: { accept: 'application/json' } });
    if (!res.ok) throw new IdentityRefused('unknown_key', `JWKS fetch returned ${res.status}`);
    this.#load(await res.json());
  }

  async get(kid) {
    if (this.#url && (this.#fetchedAt === 0 || Date.now() - this.#fetchedAt > this.#lifespanMs)) await this.#refresh();
    if (!this.#keys.has(kid) && this.#url && Date.now() - this.#fetchedAt > 30_000) await this.#refresh();
    const jwk = this.#keys.get(kid);
    if (!jwk) throw new IdentityRefused('unknown_key', `no key '${kid}' in the JWKS`);
    return jwk;
  }
}

const cryptoKeys = new WeakMap();
async function importKey(jwk) {
  let key = cryptoKeys.get(jwk);
  if (!key) {
    const { kty, crv, x, y } = jwk;
    key = await subtle.importKey('jwk', { kty, crv, x, y }, { name: 'ECDSA', namedCurve: 'P-256' }, false, ['verify']);
    cryptoKeys.set(jwk, key);
  }
  return key;
}

/**
 * Verify one note and return its claims, or throw `IdentityRefused`.
 * @param {string | null | undefined} token
 * @param {{audience: string, keys: object | string | KeySet, issuer?: string, now?: number, leeway?: number}} options
 */
export async function verify(token, { audience, keys, issuer, now, leeway = DEFAULT_LEEWAY_SECONDS }) {
  if (token === null || token === undefined || token === '') throw new IdentityRefused('missing', `no ${IDENTITY_HEADER} header`);
  if (typeof token !== 'string') throw new IdentityRefused('malformed', 'token is not a string');
  const parts = token.split('.');
  if (parts.length !== 3) throw new IdentityRefused('malformed', `expected 3 segments, got ${parts.length}`);
  const header = parseJson(b64url(parts[0]), 'header');
  if (header.typ !== IDENTITY_TYP) throw new IdentityRefused('wrong_type', `typ is '${header.typ}', not '${IDENTITY_TYP}'`);
  if (header.alg !== IDENTITY_ALG) throw new IdentityRefused('wrong_algorithm', `alg is '${header.alg}'`);
  if (typeof header.kid !== 'string' || header.kid === '') throw new IdentityRefused('unknown_key', 'no kid in the header');
  const keySet = keys instanceof KeySet ? keys : new KeySet(keys);
  const jwk = await keySet.get(header.kid);
  const signature = b64url(parts[2]);
  if (signature.length !== 64) throw new IdentityRefused('bad_signature', 'ES256 signature must be 64 bytes');
  const ok = await subtle.verify(
    { name: 'ECDSA', hash: 'SHA-256' },
    await importKey(jwk),
    signature,
    Buffer.from(`${parts[0]}.${parts[1]}`, 'ascii'),
  );
  if (!ok) throw new IdentityRefused('bad_signature', 'signature does not verify');
  const claims = parseJson(b64url(parts[1]), 'payload');
  return checkClaims(claims, { audience, issuer, now, leeway });
}

/** The claim checks, after the signature. Same order as the Python helper. */
export function checkClaims(claims, { audience, issuer, now, leeway = DEFAULT_LEEWAY_SECONDS }) {
  if (claims.aud !== audience) throw new IdentityRefused('wrong_audience', `note is for '${claims.aud}'`);
  if (issuer !== undefined && claims.iss !== issuer) throw new IdentityRefused('wrong_issuer', `note is from '${claims.iss}'`);
  if (!isInt(claims.iat)) throw new IdentityRefused('bad_claims', 'iat is not an integer');
  if (!isInt(claims.exp)) throw new IdentityRefused('bad_claims', 'exp is not an integer');
  if (claims.exp - claims.iat > MAX_TTL_SECONDS) throw new IdentityRefused('ttl_too_long', `exp - iat is ${claims.exp - claims.iat}s`);
  const at = now ?? Math.floor(Date.now() / 1000);
  if (at >= claims.exp + leeway) throw new IdentityRefused('expired', `expired ${at - claims.exp}s ago`);
  if (claims.iat > at + leeway) throw new IdentityRefused('not_yet_valid', `issued ${claims.iat - at}s in the future`);
  return validateNote(claims);
}

function bad(message) {
  throw new IdentityRefused('bad_claims', message);
}

/** Shape checks mirroring `ssc_contracts.identity.IdentityNote`. Returns a frozen note. */
export function validateNote(c) {
  for (const k of Object.keys(c)) if (!CLAIMS.has(k)) bad(`unknown claim '${k}'`);
  for (const k of ['iss', 'aud', 'sub', 'org', 'app', 'env', 'role']) if (typeof c[k] !== 'string') bad(`${k} missing or not a string`);
  if (!RE.iss.test(c.iss)) bad('iss is not a cell issuer');
  if (!RE.aud.test(c.aud)) bad('aud is not an origin');
  if (!RE.sub.test(c.sub)) bad('sub is not a usr_ or sch_ id');
  if (!RE.org.test(c.org)) bad('org is not an org_ id');
  if (!RE.app.test(c.app)) bad('app is not an app_ id');
  if (!ENVS.has(c.env)) bad(`env '${c.env}' is not prod or preview`);
  if (!ROLES.has(c.role)) bad(`role '${c.role}' is unknown`);
  if (!isInt(c.iat) || !isInt(c.exp) || c.iat < 0 || c.exp < 0) bad('iat/exp are not non-negative integers');
  if (c.exp <= c.iat) bad('exp must be after iat');
  if (c.exp - c.iat > MAX_TTL_SECONDS) bad(`a note lives at most ${MAX_TTL_SECONDS} seconds`);
  const groups = c.groups ?? [];
  if (!Array.isArray(groups)) bad('groups is not a list');
  if (groups.length > MAX_GROUPS) bad(`more than ${MAX_GROUPS} groups`);
  for (const g of groups) if (typeof g !== 'string' || !RE.grp.test(g)) bad(`not a group id: '${g}'`);
  if (new Set(groups).size !== groups.length) bad('groups repeat');
  if (c.name !== undefined && (typeof c.name !== 'string' || c.name.length < 1 || c.name.length > 256)) bad('name is not a short string');
  if (c.email !== undefined && (typeof c.email !== 'string' || c.email.length > 320 || !RE.email.test(c.email))) bad('email is not an address');
  const isSchedule = c.sub.startsWith('sch_');
  if (isSchedule) {
    if (c.role !== 'schedule') bad("a schedule subject carries role 'schedule'");
    if (c.name !== undefined || c.email !== undefined) bad('a schedule note carries no name or email');
  } else if (c.role === 'schedule') {
    bad("only a schedule subject carries role 'schedule'");
  }
  return Object.freeze({
    iss: c.iss, aud: c.aud, sub: c.sub, iat: c.iat, exp: c.exp, org: c.org, app: c.app, env: c.env, role: c.role,
    groups: Object.freeze([...groups]),
    name: c.name ?? null,
    email: c.email ?? null,
    isSchedule,
  });
}

/** Pull the note from a headers object (Node `IncomingMessage.headers`, a `Headers`, or a plain object). */
export function tokenFromHeaders(headers) {
  if (!headers) return null;
  if (typeof headers.get === 'function') return headers.get(IDENTITY_HEADER) ?? null;
  const wanted = IDENTITY_HEADER.toLowerCase();
  for (const [name, value] of Object.entries(headers)) {
    if (name.toLowerCase() === wanted) return Array.isArray(value) ? value[0] : value;
  }
  return null;
}

/** One verifier per app process: holds the audience and the key source. */
export class IdentityVerifier {
  #keys;
  constructor({ audience, keys, issuer, leeway = DEFAULT_LEEWAY_SECONDS }) {
    if (typeof audience !== 'string') throw new TypeError('audience is required: this app\'s exact origin');
    this.audience = audience;
    this.issuer = issuer;
    this.leeway = leeway;
    this.#keys = new KeySet(keys);
  }

  verify(token, { now } = {}) {
    return verify(token, { audience: this.audience, keys: this.#keys, issuer: this.issuer, now, leeway: this.leeway });
  }

  /** Verify the note in `headers`; refuses with `missing` when the gateway did not attach one. */
  fromHeaders(headers, { now } = {}) {
    return this.verify(tokenFromHeaders(headers), { now });
  }
}
