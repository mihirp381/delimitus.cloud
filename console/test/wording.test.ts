import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const SRC = join(import.meta.dirname, '../src');

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? sources(join(dir, e.name)) : /\.tsx?$/.test(e.name) && e.name !== 'schema.d.ts' ? [join(dir, e.name)] : [],
  );
}

describe('wording', () => {
  it('says Rejected, never Denied', () => {
    const files = sources(SRC);
    expect(files.length).toBeGreaterThan(20);
    expect(files.filter((f) => /Denied/.test(readFileSync(f, 'utf8'))).map((f) => f.slice(SRC.length + 1))).toEqual([]);
  });
});
