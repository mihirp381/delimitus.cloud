import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { describe, expect, it } from 'vitest';
import { AA_THRESHOLD, contrastRatio, meetsAA } from '../src/tokens/contrast';
import { emitCss, tokenVar } from '../src/tokens/emit';
import { PRIMITIVE_SCALE_NAMES } from '../src/tokens/primitives';
import {
  CONTRAST_PAIRS,
  EFFECTS,
  SHIPPING_THEMES,
  THEME_TOKENS,
  type SemanticTokens,
} from '../src/tokens/semantic';

const ROOT = join(import.meta.dirname, '..');
const SRC = join(ROOT, 'src');
const TOKENS_DIR = join(SRC, 'tokens');
const CSS_PATH = join(TOKENS_DIR, 'tokens.css');
const TOKEN_NAMES = Object.keys(THEME_TOKENS.light) as (keyof SemanticTokens)[];

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const path = join(dir, e.name);
    if (e.isDirectory()) return path === TOKENS_DIR ? [] : sourceFiles(path);
    return /\.(tsx?|css)$/.test(e.name) && !e.name.endsWith('.d.ts') ? [path] : [];
  });
}

describe('contrast', () => {
  it('computes the WCAG ratio', () => {
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 5);
    expect(contrastRatio('#777777', '#777777')).toBe(1);
    expect(AA_THRESHOLD['body-text']).toBe(4.5);
  });

  for (const theme of SHIPPING_THEMES) {
    it(`clears AA for every pair in the ${theme} theme`, () => {
      const t = THEME_TOKENS[theme];
      const failures = CONTRAST_PAIRS.filter((p) => !meetsAA(t[p.fg], t[p.bg], p.use)).map(
        (p) => `${p.fg} on ${p.bg} (${p.use}): ${contrastRatio(t[p.fg], t[p.bg]).toFixed(2)}:1`,
      );
      expect(failures).toEqual([]);
    });
  }

  it('holds body text to at least 4.5:1', () => {
    const body = CONTRAST_PAIRS.filter((p) => p.use === 'body-text');
    expect(body.length).toBeGreaterThan(10);
    for (const theme of SHIPPING_THEMES) {
      const t = THEME_TOKENS[theme];
      for (const p of body) expect(contrastRatio(t[p.fg], t[p.bg])).toBeGreaterThanOrEqual(4.5);
    }
  });

  it('checks or excuses in writing every token', () => {
    const covered = new Set(CONTRAST_PAIRS.flatMap((p) => [p.fg, p.bg]));
    expect(TOKEN_NAMES.filter((n) => !covered.has(n))).toEqual([]);
    const unexplained = CONTRAST_PAIRS.filter((p) => p.use === 'decorative' && !p.why?.trim());
    expect(unexplained).toEqual([]);
  });
});

describe('tokens.css', () => {
  it('is the output of emit.ts', () => {
    const expected = emitCss();
    if (process.env.SSC_WRITE_TOKENS === '1') writeFileSync(CSS_PATH, expected);
    expect(readFileSync(CSS_PATH, 'utf8'), 'stale: run `npm run gen:tokens`').toBe(expected);
  });

  it('binds every token in the light, dark and system-dark blocks', () => {
    const css = emitCss();
    const blocks = [
      css.split(":root,\n[data-theme='light'] {")[1]?.split('}')[0] ?? '',
      css.split("[data-theme='dark'] {")[1]?.split('}')[0] ?? '',
      css.split(":root:not([data-theme='light']) {")[1]?.split('}')[0] ?? '',
    ];
    const names = [...TOKEN_NAMES, ...Object.keys(EFFECTS.light)].map(tokenVar);
    for (const block of blocks) {
      expect(names.filter((n) => !block.includes(`${n}:`))).toEqual([]);
    }
    const lines = (block: string | undefined) =>
      (block ?? '').split('\n').map((l) => l.trim()).filter(Boolean);
    expect(lines(blocks[1])).toEqual(lines(blocks[2]));
  });
});

describe('components use only semantic tokens', () => {
  const files = sourceFiles(SRC);

  it('scans the source', () => {
    expect(files.length).toBeGreaterThan(10);
    expect(files.some((f) => f.endsWith('styles.css'))).toBe(true);
  });

  it('has no colour literal, primitive import or primitive name outside src/tokens', () => {
    const scale = new RegExp(`\\b(${PRIMITIVE_SCALE_NAMES.join('|')})\\b`);
    const offenders = files.flatMap((f) => {
      const text = readFileSync(f, 'utf8');
      const found: string[] = [];
      if (/#[0-9a-fA-F]{3,8}\b/.test(text)) found.push('hex colour');
      if (/\b(rgba?|hsla?|oklch|oklab|lab|lch|hwb)\(/.test(text)) found.push('colour function');
      if (/tokens\/primitives/.test(text)) found.push('primitives import');
      if (scale.test(text)) found.push('primitive scale name');
      return found.map((what) => `${relative(ROOT, f)}: ${what}`);
    });
    expect(offenders).toEqual([]);
  });

  it('refers only to custom properties tokens.css defines', () => {
    const css = readFileSync(CSS_PATH, 'utf8');
    const defined = new Set([...css.matchAll(/(--ssc-[a-z0-9-]+):/g)].map((m) => m[1]));
    const used = files.flatMap((f) =>
      [...readFileSync(f, 'utf8').matchAll(/var\((--ssc-[a-z0-9-]+)\)/g)].map((m) => [f, m[1]]),
    );
    expect(used.length).toBeGreaterThan(20);
    const unknown = used.filter(([, name]) => !defined.has(name));
    expect(unknown.map(([f, n]) => `${relative(ROOT, f ?? '')}: ${n}`)).toEqual([]);
  });
});
