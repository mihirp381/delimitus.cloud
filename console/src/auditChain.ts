/**
 * Checks an audit export's hash chain in the browser, with no API call: a port of the CLI's
 * `ssc audit verify` (`packages/ssc_cli/src/ssc_cli/audit_chain.py`), which says what each check
 * is. `test/audit-chain.test.ts` holds it to that code's own answers, file by file.
 *
 * Each JSON-lines row carries `prev_hash` and `hash` in hex and its `canonical` bytes in base64.
 * For each row, in file order: its seq follows the previous row's (`missing`), its `prev_hash`
 * is the previous row's hash (`prev_link`), `sha256(prev_hash || canonical)` is its hash
 * (`hash`), and the canonical bytes are ssc-audit-v1 JSON saying what the row says (`fields`).
 * The first row links to genesis (32 zero bytes) when it is seq 1; an export that starts later
 * is checked from its first row, and the report says so.
 *
 * The canonical form is Python's `json.dumps(sort_keys=True, separators=(",", ":"),
 * ensure_ascii=False)`, so JSON is read and written here the way Python does it rather than
 * with `JSON.parse`: whole numbers of any size stay exact, `1.0` stays a decimal and is written
 * `1.0`, keys sort by code point. Two things are stricter than the CLI, and neither is a form
 * the API writes: a row's `at` must be `YYYY-MM-DD`, any one character, `HH:MM[:SS[.ffffff]]`
 * and `Z` or an offset (Python also takes week dates and forms without separators), and JSON
 * nested more than 500 deep is not read.
 */

export type BreakCause = 'unreadable' | 'missing' | 'prev_link' | 'hash' | 'fields';

export interface ChainReport {
  readonly ok: boolean;
  /** Rows that passed every check, before the broken one if there is one. */
  readonly checked: number;
  readonly orgId: string | null;
  readonly firstSeq: bigint | null;
  readonly lastSeq: bigint | null;
  /** Hex. The hash of the last row that passed. */
  readonly lastHash: string | null;
  /** The file starts at seq 1, so the chain was checked from its first link. */
  readonly fromGenesis: boolean;
  readonly brokenLine: number | null;
  readonly brokenSeq: bigint | null;
  readonly cause: BreakCause | null;
}

const HASH_LENGTH = 32;
const MAX_DEPTH = 500;
const V1_KEYS = ['org_id', 'seq', 'at', 'action', 'actor', 'target', 'before', 'after', 'policy_decision_id'];
const ACTOR_KEYS = ['kind', 'id', 'via_agent', 'client_id', 'ip'];
const TARGET_KEYS = ['kind', 'id'];

/** JSON as Python reads it: `bigint` is a whole number, `number` a decimal one. */
type Json = null | boolean | bigint | number | string | readonly Json[] | JsonObject;
type JsonObject = ReadonlyMap<string, Json>;

class NotJson extends Error {}

const WHITESPACE = new Set([' ', '\t', '\n', '\r']);
const NUMBER = /(-?(?:0|[1-9][0-9]*))(\.[0-9]+)?([eE][-+]?[0-9]+)?/y;
const LITERALS: readonly (readonly [string, Json])[] = [
  ['null', null],
  ['true', true],
  ['false', false],
  ['NaN', Number.NaN],
  ['Infinity', Number.POSITIVE_INFINITY],
  ['-Infinity', Number.NEGATIVE_INFINITY],
];
const ESCAPES: Readonly<Record<string, string>> = {
  '"': '"',
  '\\': '\\',
  '/': '/',
  b: '\b',
  f: '\f',
  n: '\n',
  r: '\r',
  t: '\t',
};

/** What Python's `json.loads` makes of `text`; throws `NotJson` where it raises. */
function parseJson(text: string): Json {
  let at = 0;

  function space(): void {
    while (WHITESPACE.has(text[at] ?? '')) at += 1;
  }

  function string(): string {
    at += 1;
    let out = '';
    for (;;) {
      const c = text[at];
      if (c === undefined || c < ' ') throw new NotJson();
      at += 1;
      if (c === '"') return out;
      if (c !== '\\') {
        out += c;
        continue;
      }
      const e = text[at];
      at += 1;
      if (e === 'u') {
        const hex = text.slice(at, at + 4);
        if (!/^[0-9a-fA-F]{4}$/.test(hex)) throw new NotJson();
        out += String.fromCharCode(Number.parseInt(hex, 16));
        at += 4;
      } else if (e !== undefined && Object.hasOwn(ESCAPES, e)) {
        out += ESCAPES[e];
      } else {
        throw new NotJson();
      }
    }
  }

  function value(depth: number): Json {
    if (depth > MAX_DEPTH) throw new NotJson();
    const c = text[at];
    if (c === '"') return string();
    if (c === '{') {
      const out = new Map<string, Json>();
      at += 1;
      space();
      if (text[at] === '}') {
        at += 1;
        return out;
      }
      for (;;) {
        if (text[at] !== '"') throw new NotJson();
        const key = string();
        space();
        if (text[at] !== ':') throw new NotJson();
        at += 1;
        space();
        // A key given twice keeps its last value, as in Python.
        out.set(key, value(depth + 1));
        space();
        const next = text[at];
        at += 1;
        if (next === '}') return out;
        if (next !== ',') throw new NotJson();
        space();
      }
    }
    if (c === '[') {
      const out: Json[] = [];
      at += 1;
      space();
      if (text[at] === ']') {
        at += 1;
        return out;
      }
      for (;;) {
        out.push(value(depth + 1));
        space();
        const next = text[at];
        at += 1;
        if (next === ']') return out;
        if (next !== ',') throw new NotJson();
        space();
      }
    }
    for (const [word, meaning] of LITERALS) {
      if (text.startsWith(word, at)) {
        at += word.length;
        return meaning;
      }
    }
    NUMBER.lastIndex = at;
    const number = NUMBER.exec(text);
    if (!number) throw new NotJson();
    at += number[0].length;
    return number[2] === undefined && number[3] === undefined ? BigInt(number[0]) : Number(number[0]);
  }

  space();
  const out = value(0);
  space();
  if (at !== text.length) throw new NotJson();
  return out;
}

/** Python's `repr` of a float, which is how `json.dumps` writes one. */
function pythonFloat(x: number): string {
  if (Number.isNaN(x)) return 'NaN';
  if (!Number.isFinite(x)) return x > 0 ? 'Infinity' : '-Infinity';
  const sign = x < 0 || Object.is(x, -0) ? '-' : '';
  if (x === 0) return `${sign}0.0`;
  // Both languages give the shortest digits that read back as `x`; only the layout differs.
  const [mantissa = '', exponent = '0'] = String(Math.abs(x)).split('e');
  const [whole = '', fraction = ''] = mantissa.split('.');
  const padded = whole + fraction;
  const leading = padded.length - padded.replace(/^0+/, '').length;
  const digits = padded.slice(leading).replace(/0+$/, '');
  // The value is 0.<digits> times ten to the `point`.
  const point = whole.length + Number(exponent) - leading;
  if (point > 16 || point <= -4) {
    const e = point - 1;
    const rest = digits.length > 1 ? `.${digits.slice(1)}` : '';
    return `${sign}${digits[0]}${rest}e${e < 0 ? '-' : '+'}${String(Math.abs(e)).padStart(2, '0')}`;
  }
  if (point <= 0) return `${sign}0.${'0'.repeat(-point)}${digits}`;
  if (point >= digits.length) return `${sign}${digits}${'0'.repeat(point - digits.length)}.0`;
  return `${sign}${digits.slice(0, point)}.${digits.slice(point)}`;
}

const NAMED_ESCAPES: Readonly<Record<string, string>> = {
  '"': '\\"',
  '\\': '\\\\',
  '\b': '\\b',
  '\f': '\\f',
  '\n': '\\n',
  '\r': '\\r',
  '\t': '\\t',
};

function pythonString(s: string): string {
  let out = '"';
  for (const c of s) {
    if (Object.hasOwn(NAMED_ESCAPES, c)) out += NAMED_ESCAPES[c];
    else if (c < ' ') out += `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`;
    else out += c;
  }
  return `${out}"`;
}

/** Order by code point, as Python sorts text; JavaScript's own order is by UTF-16 unit. */
function byCodePoint(a: string, b: string): number {
  const left = Array.from(a);
  const right = Array.from(b);
  for (let i = 0; i < left.length && i < right.length; i += 1) {
    const diff = (left[i]?.codePointAt(0) ?? 0) - (right[i]?.codePointAt(0) ?? 0);
    if (diff !== 0) return diff;
  }
  return left.length - right.length;
}

/** The canonical text of `value`: sorted keys, no whitespace, nothing escaped that need not be. */
function canonicalText(value: Json): string {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'bigint') return value.toString();
  if (typeof value === 'number') return pythonFloat(value);
  if (typeof value === 'string') return pythonString(value);
  if (value instanceof Map) {
    const keys = Array.from((value as JsonObject).keys()).sort(byCodePoint);
    return `{${keys.map((k) => `${pythonString(k)}:${canonicalText((value as JsonObject).get(k) ?? null)}`).join(',')}}`;
  }
  return `[${(value as readonly Json[]).map(canonicalText).join(',')}]`;
}

function isObject(value: Json | undefined): value is JsonObject {
  return value instanceof Map;
}

function kind(value: Json): string {
  if (value === null) return 'null';
  if (isObject(value)) return 'object';
  return Array.isArray(value) ? 'list' : typeof value;
}

function isNumeric(value: Json): value is boolean | bigint | number {
  return typeof value === 'boolean' || typeof value === 'bigint' || typeof value === 'number';
}

/** Python's `==` on parsed JSON: numbers by value whatever their type, containers member by member. */
function equal(a: Json, b: Json): boolean {
  if (isNumeric(a) && isNumeric(b)) {
    if (typeof a === 'number' && typeof b === 'number') return a === b;
    if (typeof a === 'number') return Number.isInteger(a) && BigInt(a) === BigInt(b);
    if (typeof b === 'number') return Number.isInteger(b) && BigInt(a) === BigInt(b);
    return BigInt(a) === BigInt(b);
  }
  if (kind(a) !== kind(b)) return false;
  if (isObject(a) && isObject(b)) {
    if (a.size !== b.size) return false;
    for (const [key, left] of a) {
      const right = b.get(key);
      if (right === undefined || !equal(left, right)) return false;
    }
    return true;
  }
  if (Array.isArray(a) && Array.isArray(b)) {
    const [left, right] = [a as readonly Json[], b as readonly Json[]];
    return left.length === right.length && left.every((item, i) => equal(item, right[i] ?? null));
  }
  return a === b;
}

/** Equal and of the same JSON type (`true` is not `1`). */
function same(a: Json, b: Json | undefined): boolean {
  return b !== undefined && kind(a) === kind(b) && equal(a, b);
}

function sameJson(a: Json, b: Json | undefined): boolean {
  return canonicalText(a) === canonicalText(b ?? null);
}

const AT =
  /^(\d{4})-(\d{2})-(\d{2})[^](\d{2})(?::(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?(?:(Z)|([+-])(\d{2})(?::?(\d{2})(?::?(\d{2})(?:[.,](\d+))?)?)?)$/u;
const DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

function micros(fraction: string | undefined): number {
  return Number((fraction ?? '').slice(0, 6).padEnd(6, '0'));
}

/** The instant an ISO 8601 time with an offset names, in microseconds; null without one. */
function instant(text: string): bigint | null {
  const m = AT.exec(text);
  if (!m) return null;
  const [year, month, day, hour, minute, second] = [m[1], m[2], m[3], m[4], m[5], m[6]].map((part) =>
    Number(part ?? '0'),
  ) as [number, number, number, number, number, number];
  const micro = micros(m[7]);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = month === 2 && leap ? 29 : (DAYS_IN_MONTH[month - 1] ?? 0);
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > days) return null;
  // 24:00 is the end of the day, and only that.
  const endOfDay = hour === 24 && minute === 0 && second === 0 && micro === 0;
  if ((hour > 23 && !endOfDay) || minute > 59 || second > 59) return null;
  let offset = 0n;
  if (m[8] === undefined) {
    const [h, min, s] = [m[10], m[11], m[12]].map((part) => Number(part ?? '0')) as [number, number, number];
    if (min > 59 || s > 59) return null;
    offset = BigInt(h * 3600 + min * 60 + s) * 1_000_000n + BigInt(micros(m[13]));
    if (offset >= 86_400_000_000n) return null;
    if (m[9] === '-') offset = -offset;
  }
  // Days since 1970-01-01 in the proleptic Gregorian calendar.
  const y = month <= 2 ? year - 1 : year;
  const era = Math.floor(y / 400);
  const yearOfEra = y - era * 400;
  const dayOfYear = Math.floor((153 * (month + (month > 2 ? -3 : 9)) + 2) / 5) + day - 1;
  const dayOfEra = yearOfEra * 365 + Math.floor(yearOfEra / 4) - Math.floor(yearOfEra / 100) + dayOfYear;
  const epochDay = era * 146_097 + dayOfEra - 719_468;
  const seconds = BigInt(epochDay) * 86_400n + BigInt(hour * 3600 + minute * 60 + second);
  return seconds * 1_000_000n + BigInt(micro) - offset;
}

function sameAt(a: Json, b: Json | undefined): boolean {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const [left, right] = [instant(a), instant(b)];
  return left !== null && right !== null && left === right;
}

function withKeys(value: Json | undefined, keys: readonly string[]): JsonObject | null {
  if (!isObject(value)) return null;
  return value.size === keys.length && keys.every((k) => value.has(k)) ? value : null;
}

function sameBytes(a: Uint8Array, b: Uint8Array): boolean {
  return a.length === b.length && a.every((byte, i) => byte === b[i]);
}

/** The canonical document's org when it is v1 JSON describing exactly `row`, else null. */
function fieldsMatch(raw: Uint8Array, row: JsonObject): string | null {
  let parsed: Json;
  try {
    // Kept, a byte order mark is not JSON; dropped, it would hide three bytes from the check.
    parsed = parseJson(new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(raw));
  } catch {
    return null;
  }
  const doc = withKeys(parsed, V1_KEYS);
  if (doc === null || !sameBytes(new TextEncoder().encode(canonicalText(doc)), raw)) return null;
  const field = (name: string): Json => doc.get(name) ?? null;
  const actor = withKeys(field('actor'), ACTOR_KEYS);
  const target = withKeys(field('target'), TARGET_KEYS);
  const org = field('org_id');
  const shownActor = row.get('actor');
  const shownTarget = row.get('target');
  if (actor === null || target === null || typeof org !== 'string') return null;
  if (!isObject(shownActor) || !isObject(shownTarget)) return null;
  const of = (object: JsonObject, name: string): Json => object.get(name) ?? null;
  // An absent key reads as null, as Python's `dict.get` gives None.
  const shown = (object: JsonObject, name: string): Json => object.get(name) ?? null;
  const matches =
    same(field('seq'), shown(row, 'seq')) &&
    sameAt(field('at'), shown(row, 'at')) &&
    same(field('action'), shown(row, 'action')) &&
    same(of(actor, 'kind'), shown(shownActor, 'kind')) &&
    same(of(actor, 'id'), shown(shownActor, 'id')) &&
    same(of(actor, 'via_agent'), shown(shownActor, 'via_agent')) &&
    same(of(actor, 'client_id'), shown(shownActor, 'client_id')) &&
    same(of(target, 'kind'), shown(shownTarget, 'kind')) &&
    same(of(target, 'id'), shown(shownTarget, 'id')) &&
    sameJson(field('before'), shown(row, 'before')) &&
    sameJson(field('after'), shown(row, 'after')) &&
    same(field('policy_decision_id'), shown(row, 'policy_decision_id'));
  return matches ? org : null;
}

const HEX_SPACE = new Set([' ', '\t', '\n', '\r', '\v', '\f']);

/** Python's `bytes.fromhex`: pairs of hex digits, with white space allowed between pairs. */
function fromHex(text: string): Uint8Array | null {
  const out: number[] = [];
  let at = 0;
  for (;;) {
    while (HEX_SPACE.has(text[at] ?? '')) at += 1;
    if (at >= text.length) return Uint8Array.from(out);
    const pair = text.slice(at, at + 2);
    if (!/^[0-9a-fA-F]{2}$/.test(pair)) return null;
    out.push(Number.parseInt(pair, 16));
    at += 2;
  }
}

const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;

/** Python's `base64.b64decode(validate=True)`: the standard alphabet, padded, nothing else. */
function fromBase64(text: string): Uint8Array | null {
  if (!BASE64.test(text)) return null;
  return Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
}

function toHex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

interface Row {
  readonly row: JsonObject;
  readonly seq: bigint;
  readonly prev: Uint8Array;
  readonly digest: Uint8Array;
  readonly canonical: Uint8Array;
}

/** The row, its prev_hash, hash and canonical bytes; null when the line is not one. */
function readRow(line: string): Row | null {
  let parsed: Json;
  try {
    parsed = parseJson(line);
  } catch {
    return null;
  }
  if (!isObject(parsed)) return null;
  const [prevHex, digestHex, encoded, seq] = ['prev_hash', 'hash', 'canonical', 'seq'].map((k) => parsed.get(k));
  if (typeof prevHex !== 'string' || typeof digestHex !== 'string' || typeof encoded !== 'string') return null;
  const prev = fromHex(prevHex);
  const digest = fromHex(digestHex);
  const canonical = fromBase64(encoded);
  if (prev === null || digest === null || canonical === null) return null;
  if (prev.length !== HASH_LENGTH || digest.length !== HASH_LENGTH) return null;
  return typeof seq === 'bigint' ? { row: parsed, seq, prev, digest, canonical } : null;
}

async function sha256(prev: Uint8Array, canonical: Uint8Array): Promise<Uint8Array> {
  if (!globalThis.crypto?.subtle) {
    // Browsers offer it only to pages served over HTTPS (or from localhost).
    throw new Error('This browser cannot compute SHA-256 on this page, so the chain was not checked.');
  }
  const joined = new Uint8Array(prev.length + canonical.length);
  joined.set(prev);
  joined.set(canonical, prev.length);
  return new Uint8Array(await crypto.subtle.digest('SHA-256', joined));
}

/** Whether the row follows the one before it, or genesis when it is the first and seq 1. */
function link(row: Row, lastSeq: bigint | null, lastHash: Uint8Array | null): BreakCause | null {
  if (lastSeq === null || lastHash === null) {
    return row.seq === 1n && row.prev.some((byte) => byte !== 0) ? 'prev_link' : null;
  }
  if (row.seq !== lastSeq + 1n) return 'missing';
  return sameBytes(row.prev, lastHash) ? null : 'prev_link';
}

// What Python's `str.strip` strips, which is not quite what `trim` does.
const BLANK = /^[\t\n\v\f\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/;

/**
 * Walks the export's rows in order and stops at the first that fails a check. `file` is the
 * export's bytes as saved; they are read, never changed.
 */
export async function checkChain(file: Uint8Array): Promise<ChainReport> {
  let orgId: string | null = null;
  let firstSeq: bigint | null = null;
  let lastSeq: bigint | null = null;
  let lastHash: Uint8Array | null = null;
  let fromGenesis = false;
  let checked = 0;

  const report = (broken?: readonly [number, bigint | null, BreakCause]): ChainReport => ({
    ok: broken === undefined,
    checked,
    orgId,
    firstSeq,
    lastSeq,
    lastHash: lastHash === null ? null : toHex(lastHash),
    fromGenesis,
    brokenLine: broken?.[0] ?? null,
    brokenSeq: broken?.[1] ?? null,
    cause: broken?.[2] ?? null,
  });

  // As the CLI opens the file: UTF-8 with bad bytes replaced, a byte order mark kept, and a
  // line ending at \n, \r\n or \r.
  const lines = new TextDecoder('utf-8', { ignoreBOM: true }).decode(file).split(/\r\n|\r|\n/);
  for (const [index, line] of lines.entries()) {
    if (BLANK.test(line)) continue;
    const row = readRow(line);
    if (row === null) return report([index + 1, null, 'unreadable']);
    if (lastSeq === null) {
      firstSeq = row.seq;
      fromGenesis = row.seq === 1n;
    }
    let cause = link(row, lastSeq, lastHash);
    if (cause === null && !sameBytes(await sha256(row.prev, row.canonical), row.digest)) cause = 'hash';
    const rowOrg = cause === null ? fieldsMatch(row.canonical, row.row) : null;
    if (cause === null && (rowOrg === null || (orgId !== null && orgId !== rowOrg))) cause = 'fields';
    if (cause !== null) {
      const expected: bigint = lastSeq === null || cause !== 'missing' ? row.seq : lastSeq + 1n;
      return report([index + 1, expected, cause]);
    }
    orgId = rowOrg;
    lastSeq = row.seq;
    lastHash = row.digest;
    checked += 1;
  }
  return report();
}

/** The CLI's words for each way a chain breaks. */
export const CAUSES: Readonly<Record<BreakCause, string>> = {
  unreadable: 'the line is not an audit row with hex hashes and base64 canonical bytes',
  missing: 'an event is missing before this line',
  prev_link: "its prev_hash is not the previous event's hash",
  hash: 'sha256(prev_hash || canonical) is not its hash',
  fields: 'its canonical bytes do not say what the row says',
};

export interface ChainVerdict {
  readonly tone: 'success' | 'info' | 'warning' | 'danger';
  readonly headline: string;
  readonly lines: readonly string[];
  readonly lastHash: string | null;
}

function before(checked: number): string {
  if (checked === 0) return 'No event before it was checked.';
  return checked === 1 ? 'The 1 event before it checks out.' : `The ${checked} events before it check out.`;
}

/**
 * The report in the words `ssc audit verify` uses. `rowFilters` says the export was made with a
 * filter that leaves events out of the middle (an action, an actor or a target): its seqs skip,
 * which is the filter's doing and not a break.
 */
export function chainVerdict(fileName: string, report: ChainReport, rowFilters = false): ChainVerdict {
  if (report.ok && report.checked === 0) {
    return { tone: 'info', headline: `${fileName} has no events.`, lines: [], lastHash: null };
  }
  if (report.ok) {
    const events = report.checked === 1 ? '1 event' : `${report.checked} events`;
    return {
      tone: 'success',
      headline: `Chain intact: ${events} of ${report.orgId}, seq ${report.firstSeq} to ${report.lastSeq}.`,
      lines: [
        report.fromGenesis
          ? 'The file starts at seq 1, so every link from the first event was checked.'
          : `The file starts at seq ${report.firstSeq}, so the links before it were not checked. ` +
            'Export with no From or Until to check the whole chain.',
      ],
      lastHash: report.lastHash,
    };
  }
  const where = `line ${report.brokenLine}${report.brokenSeq === null ? '' : ` (seq ${report.brokenSeq})`}`;
  const cause = CAUSES[report.cause ?? 'unreadable'];
  if (report.cause === 'missing' && rowFilters) {
    return {
      tone: 'warning',
      headline: 'This export is filtered by action, actor or target, so it leaves events out and its chain cannot be checked.',
      lines: [
        `The check stopped at ${where}: ${cause}. ${before(report.checked)}`,
        'Export with only From and Until, or with no filters, to check the chain.',
      ],
      lastHash: null,
    };
  }
  return {
    tone: 'danger',
    headline: `Chain broken at ${where}: ${cause}.`,
    lines: [
      before(report.checked),
      ...(report.cause === 'missing'
        ? ['An export filtered by action, actor or target leaves events out and reads like this.']
        : []),
    ],
    lastHash: null,
  };
}
