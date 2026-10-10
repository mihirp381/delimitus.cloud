import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { describe, expect, it } from 'vitest';
import { AA_THRESHOLD, contrastRatio, luminance, meetsAA } from '../src/tokens/contrast';
import { emitCss, sharedTokens, tokenVar } from '../src/tokens/emit';
import { PRIMITIVE_SCALE_NAMES } from '../src/tokens/primitives';
import {
  CONTRAST_PAIRS,
  EFFECTS,
  FEEDBACK,
  FONT_FAMILIES,
  GRADIENTS,
  RADIUS,
  SHIPPING_THEMES,
  TEXT_SURFACES,
  THEME_TOKENS,
  TONES,
  TYPE,
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

/** Whether `fg` on `bg` is in the list the themes are held to, as body text. */
function listed(fg: keyof SemanticTokens, bg: keyof SemanticTokens): boolean {
  return CONTRAST_PAIRS.some((p) => p.fg === fg && p.bg === bg && p.use === 'body-text');
}

describe('the landing page\'s colours (GA-10.18)', () => {
  // Text on each of these must hold 4.5:1 in every theme, and the pair must be in
  // CONTRAST_PAIRS so the tests above keep holding it.
  const text: [keyof SemanticTokens, keyof SemanticTokens][] = [
    // Each tone as a section label on any surface, and as a tinted pill on its own tint.
    ...TONES.flatMap((t): [keyof SemanticTokens, keyof SemanticTokens][] => [
      [`tone${t}`, `tone${t}Soft`],
      ...TEXT_SURFACES.map((bg): [keyof SemanticTokens, keyof SemanticTokens] => [`tone${t}`, bg]),
    ]),
    // Each state as a pill or a notice on its tint, and as plain text on any surface.
    ...FEEDBACK.flatMap((f): [keyof SemanticTokens, keyof SemanticTokens][] => [
      [`feedback${f}`, `feedback${f}Soft`],
      ['textPrimary', `feedback${f}Soft`],
      ['textSecondary', `feedback${f}Soft`],
      ...TEXT_SURFACES.map((bg): [keyof SemanticTokens, keyof SemanticTokens] => [`feedback${f}`, bg]),
    ]),
    // The accent: as text on a card, its label on the fill, and its text shade wherever a link sits.
    ['accent', 'surface'],
    ['accent', 'surfaceRaised'],
    ['accentContrast', 'accent'],
    ['accentContrast', 'accentHover'],
    ['accentSoftContrast', 'accentSoft'],
    ['accentInk', 'accentSoft'],
    ...TEXT_SURFACES.flatMap((bg): [keyof SemanticTokens, keyof SemanticTokens][] => [
      ['accentInk', bg],
      ['accentInkHover', bg],
      ['textPrimary', bg],
      ['textSecondary', bg],
      ['textMuted', bg],
    ]),
  ];

  for (const theme of SHIPPING_THEMES) {
    it(`holds every tone, state and accent pairing to AA in the ${theme} theme`, () => {
      const t = THEME_TOKENS[theme];
      expect(text.length).toBeGreaterThan(80);
      const failures = text
        .filter(([fg, bg]) => contrastRatio(t[fg], t[bg]) < AA_THRESHOLD['body-text'])
        .map(([fg, bg]) => `${fg} on ${bg}: ${contrastRatio(t[fg], t[bg]).toFixed(2)}:1`);
      expect(failures).toEqual([]);
    });
  }

  it('lists each of those pairings in CONTRAST_PAIRS', () => {
    expect(text.filter(([fg, bg]) => !listed(fg, bg)).map(([fg, bg]) => `${fg} on ${bg}`)).toEqual([]);
  });

  it('keeps the primary button and the focus ring visible against the page', () => {
    for (const theme of SHIPPING_THEMES) {
      const t = THEME_TOKENS[theme];
      for (const bg of TEXT_SURFACES) {
        expect(contrastRatio(t.accent, t[bg]), `accent on ${bg}, ${theme}`).toBeGreaterThanOrEqual(3);
        expect(contrastRatio(t.focusRing, t[bg]), `focusRing on ${bg}, ${theme}`).toBeGreaterThanOrEqual(3);
      }
    }
  });

  it('uses the landing\'s values in the light theme', () => {
    const light = THEME_TOKENS.light;
    const expected: Partial<Record<keyof SemanticTokens, string>> = {
      surfaceSunken: '#F5F5F7',
      surfaceRaised: '#FFFFFF',
      borderSubtle: '#E3E3E8',
      textPrimary: '#1D1D1F',
      textSecondary: '#424245',
      textMuted: '#6E6E73',
      accent: '#0071E3',
      accentHover: '#0062C4',
      accentInk: '#0066CC',
      accentSoft: '#E8F1FD',
      accentLine: '#B9D5F8',
      toneBlue: '#0066CC',
      toneBlueSoft: '#E8F1FD',
      toneViolet: '#5B3FE0',
      toneVioletSoft: '#F0EDFF',
      tonePurple: '#A32BC7',
      tonePurpleSoft: '#F8ECFC',
      toneMagenta: '#C8246B',
      toneMagentaSoft: '#FDEDF3',
      toneOrange: '#C4400C',
      toneOrangeSoft: '#FFF0E6',
      feedbackSuccess: '#147A38',
      feedbackSuccessSoft: '#E6F5EA',
      feedbackWarning: '#A35200',
      feedbackWarningSoft: '#FFF3E0',
      feedbackDanger: '#D70015',
      feedbackDangerSoft: '#FDECEE',
    };
    for (const [name, value] of Object.entries(expected)) {
      expect(light[name as keyof SemanticTokens].toUpperCase(), name).toBe(value);
    }
  });

  it('gives every tone a lighter step in the dark theme', () => {
    const { light, dark } = THEME_TOKENS;
    for (const t of TONES) {
      expect(luminance(dark[`tone${t}`]), `tone${t}`).toBeGreaterThan(luminance(light[`tone${t}`]));
      expect(luminance(dark[`tone${t}Soft`]), `tone${t}Soft`).toBeLessThan(luminance(light[`tone${t}Soft`]));
    }
    expect(luminance(dark.accent)).toBeGreaterThan(luminance(light.accent));
    expect(luminance(dark.accentInk)).toBeGreaterThan(luminance(light.accentInk));
  });

  it('carries the landing\'s spectrum, type, radii and shadows', () => {
    expect(GRADIENTS.spectrum.toUpperCase()).toBe(
      'LINEAR-GRADIENT(90DEG, #0A84FF 0%, #6E5BFF 22%, #C24BF0 44%, #FF378C 64%, #FF5E3A 82%, #FF9F0A 100%)',
    );
    expect(TYPE.sizeBody).toBe('17px');
    expect(TYPE.leadingBody).toBe('1.52');
    expect(TYPE.trackingBody).toBe('-0.012em');
    expect(RADIUS.xl).toBe('24px');
    expect(FONT_FAMILIES.display).toContain('SF Pro Display');
    expect(FONT_FAMILIES.sans).toContain('SF Pro Text');
    expect(FONT_FAMILIES.mono).toContain('ui-monospace');
    // The landing writes rgba(0, 0, 0, 0.04); the same values in the space-separated form.
    expect(EFFECTS.light.shadowSoft).toBe('0 1px 2px rgb(0 0 0 / 0.04), 0 12px 32px -12px rgb(0 0 0 / 0.10)');
    expect(EFFECTS.light.shadowLift).toBe(
      '0 2px 6px rgb(0 0 0 / 0.04), 0 34px 70px -34px rgb(29 29 31 / 0.28)',
    );
  });

  it('shows the spectrum once: one rule in styles.css, on no component', () => {
    const uses = sourceFiles(SRC).flatMap((f) =>
      [...readFileSync(f, 'utf8').matchAll(/var\(--ssc-spectrum\)/g)].map(() => relative(ROOT, f)),
    );
    expect(uses).toEqual(['src/styles.css']);
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

  it('binds the fonts, type, radii, spacing and the spectrum once, for both themes', () => {
    const css = emitCss();
    const shared = css.split(':root {')[1]?.split('}')[0] ?? '';
    const names = sharedTokens().map(([name]) => name);
    expect(names).toEqual(
      expect.arrayContaining([
        '--ssc-font-sans',
        '--ssc-font-display',
        '--ssc-font-mono',
        '--ssc-type-size-body',
        '--ssc-radius-xl',
        '--ssc-space-1',
        '--ssc-space-8',
        '--ssc-spectrum',
      ]),
    );
    expect(new Set(names).size).toBe(names.length);
    expect(names.filter((n) => !shared.includes(`${n}:`))).toEqual([]);
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
