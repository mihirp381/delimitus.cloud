import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { PURPOSE_MAX } from '../src/components/PageHeader';

const SRC = join(import.meta.dirname, '../src');

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? sources(join(dir, e.name)) : e.name.endsWith('.tsx') ? [join(dir, e.name)] : [],
  );
}

function attribute(text: string, at: number): string {
  if (text[at] === '"') return text.slice(at, text.indexOf('"', at + 1) + 1);
  let depth = 0;
  for (let i = at; i < text.length; i++) {
    if (text[i] === '{') depth++;
    if (text[i] === '}' && --depth === 0) return text.slice(at, i + 1);
  }
  throw new Error('unclosed purpose');
}

const purposes = sources(SRC).flatMap((file) => {
  const text = readFileSync(file, 'utf8');
  return [...text.matchAll(/\bpurpose=/g)].map((m) => ({
    file: file.slice(SRC.length + 1),
    value: attribute(text, m.index + 'purpose='.length),
  }));
});

const LITERAL = /'([^'\\]*)'|"([^"\\]*)"/g;

describe('page header purposes', () => {
  it('are found in the pages', () => {
    expect(purposes.length).toBeGreaterThanOrEqual(10);
  });

  it.each(purposes)('$file: plain strings only', ({ value }) => {
    expect(value).not.toMatch(/[<`]/);
    expect(value.replace(LITERAL, '').replace(/[{}\s?:]|\b\w+\b/g, '')).toBe('');
  });

  it.each(purposes)('$file: one sentence of at most PURPOSE_MAX characters', ({ value }) => {
    const texts = [...value.matchAll(LITERAL)].map((m) => m[1] ?? m[2] ?? '');
    expect(texts.length).toBeGreaterThan(0);
    for (const t of texts) {
      expect(t.length, t).toBeLessThanOrEqual(PURPOSE_MAX);
      expect(t, t).not.toMatch(/[.!?]\s+\S/);
    }
  });
});
