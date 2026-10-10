import { describe, expect, it } from 'vitest';
import { type ChainReport, chainVerdict, checkChain } from '../src/auditChain';
import { VECTORS, type Vector, vectorBytes as bytes } from './fixtures/auditChain';

const seq = (n: bigint | null) => (n === null ? null : Number(n));

/** The report in the CLI's shape, to compare with what Python gave. */
function asPython(report: ChainReport): Vector['report'] {
  return {
    ok: report.ok,
    checked: report.checked,
    org_id: report.orgId,
    first_seq: seq(report.firstSeq),
    last_seq: seq(report.lastSeq),
    last_hash: report.lastHash,
    from_genesis: report.fromGenesis,
    broken_line: report.brokenLine,
    broken_seq: seq(report.brokenSeq),
    cause: report.cause,
  };
}

describe('the audit chain check, against the CLI', () => {
  it('has the vectors the brief names', () => {
    expect(Object.keys(VECTORS).length).toBeGreaterThan(60);
    expect(VECTORS['good']?.report).toMatchObject({ ok: true, checked: 5, from_genesis: true });
    expect(VECTORS['tampered action']?.report).toMatchObject({ ok: false, cause: 'fields', broken_seq: 2 });
    expect(VECTORS['gap']?.report).toMatchObject({ ok: false, cause: 'missing', broken_seq: 3 });
  });

  it.each(Object.keys(VECTORS))('reports what the CLI reports: %s', async (name) => {
    expect(asPython(await checkChain(bytes(name)))).toEqual(VECTORS[name]?.report);
  });

  it('checks a whole chain from genesis', async () => {
    const report = await checkChain(bytes('good'));
    expect(report).toMatchObject({ ok: true, checked: 5, firstSeq: 1n, lastSeq: 5n, fromGenesis: true });
    expect(report.lastHash).toMatch(/^[0-9a-f]{64}$/);
  });

  it('names the row whose text was changed after it was written', async () => {
    expect(await checkChain(bytes('tampered action'))).toMatchObject({
      ok: false,
      checked: 1,
      brokenLine: 2,
      brokenSeq: 2n,
      cause: 'fields',
    });
    expect(await checkChain(bytes('tampered canonical'))).toMatchObject({ ok: false, brokenSeq: 4n, cause: 'hash' });
  });

  it('names the seq that is missing, not the one that follows the gap', async () => {
    expect(await checkChain(bytes('gap'))).toMatchObject({
      ok: false,
      checked: 2,
      brokenLine: 3,
      brokenSeq: 3n,
      cause: 'missing',
    });
  });

  it('leaves the bytes it is given as they were', async () => {
    const file = bytes('good');
    const copy = Uint8Array.from(file);
    await checkChain(file);
    expect(file).toEqual(copy);
  });

  it('keeps a seq past 2^53 exact', async () => {
    const row = (n: string) => `{"seq":${n},"prev_hash":"${'00'.repeat(32)}","hash":"${'00'.repeat(32)}","canonical":""}`;
    const report = await checkChain(new TextEncoder().encode(`${row('9007199254740993')}\n`));
    expect(report).toMatchObject({ ok: false, cause: 'hash', firstSeq: 9007199254740993n, brokenSeq: 9007199254740993n });
  });
});

describe('the verdict, in the words of ssc audit verify', () => {
  const verdict = async (name: string, rowFilters = false) =>
    chainVerdict('audit-org.jsonl', await checkChain(bytes(name)), rowFilters);

  it('says an intact chain is intact, from which seq to which, and that it starts at genesis', async () => {
    const v = await verdict('good');
    expect(v.tone).toBe('success');
    expect(v.headline).toBe('Chain intact: 5 events of org_0123456789abcdefghij, seq 1 to 5.');
    expect(v.lines).toEqual(['The file starts at seq 1, so every link from the first event was checked.']);
    expect(v.lastHash).toBe(VECTORS['good']?.report.last_hash);
  });

  it('says a file that starts later was not checked before its first row', async () => {
    const v = await verdict('starts at seq 3');
    expect(v.tone).toBe('success');
    expect(v.headline).toBe('Chain intact: 3 events of org_0123456789abcdefghij, seq 3 to 5.');
    expect(v.lines).toEqual([
      'The file starts at seq 3, so the links before it were not checked. ' +
        'Export with no From or Until to check the whole chain.',
    ]);
  });

  it('names the first broken link and how many events before it hold', async () => {
    const v = await verdict('tampered action');
    expect(v.tone).toBe('danger');
    expect(v.headline).toBe('Chain broken at line 2 (seq 2): its canonical bytes do not say what the row says.');
    expect(v.lines).toEqual(['The 1 event before it checks out.']);
    expect((await verdict('wrong prev_hash')).headline).toBe(
      "Chain broken at line 3 (seq 3): its prev_hash is not the previous event's hash.",
    );
    expect((await verdict('tampered hash')).headline).toBe(
      'Chain broken at line 2 (seq 2): sha256(prev_hash || canonical) is not its hash.',
    );
    expect((await verdict('not json')).headline).toBe(
      'Chain broken at line 3: the line is not an audit row with hex hashes and base64 canonical bytes.',
    );
    expect((await verdict('seq 1 not from genesis')).lines).toEqual(['No event before it was checked.']);
  });

  it('says a gap is a break, and what else reads like one', async () => {
    const v = await verdict('gap');
    expect(v.tone).toBe('danger');
    expect(v.headline).toBe('Chain broken at line 3 (seq 3): an event is missing before this line.');
    expect(v.lines).toEqual([
      'The 2 events before it check out.',
      'An export filtered by action, actor or target leaves events out and reads like this.',
    ]);
  });

  it('does not call a gap a break in an export it knows was filtered by row', async () => {
    const v = await verdict('gap', true);
    expect(v.tone).toBe('warning');
    expect(v.headline).toBe(
      'This export is filtered by action, actor or target, so it leaves events out and its chain cannot be checked.',
    );
    expect(v.lines[0]).toBe(
      'The check stopped at line 3 (seq 3): an event is missing before this line. The 2 events before it check out.',
    );
    // Any other break is still a break.
    expect((await verdict('tampered action', true)).tone).toBe('danger');
  });

  it('says an empty file has no events', async () => {
    expect(await verdict('empty')).toEqual({
      tone: 'info',
      headline: 'audit-org.jsonl has no events.',
      lines: [],
      lastHash: null,
    });
  });
});
