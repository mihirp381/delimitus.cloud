/**
 * WCAG 2.2 contrast, computed rather than eyeballed. Copied from the Delimitus UI tokens
 * (`frontend/ui/src/tokens/contrast.ts` at ef688e9b); `luminanceGap` is left out.
 */

export interface Rgb {
  readonly r: number;
  readonly g: number;
  readonly b: number;
}

export function parseHex(hex: string): Rgb {
  const h = hex.trim().replace(/^#/, '');
  const full = h.length === 3 ? [...h].map((c) => c + c).join('') : h;
  if (!/^[0-9a-fA-F]{6}$/.test(full)) throw new Error(`not a hex colour: ${hex}`);
  return {
    r: Number.parseInt(full.slice(0, 2), 16),
    g: Number.parseInt(full.slice(2, 4), 16),
    b: Number.parseInt(full.slice(4, 6), 16),
  };
}

function channel(v: number): number {
  const s = v / 255;
  return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

/** WCAG relative luminance, 0 (black) to 1 (white). */
export function luminance(hex: string): number {
  const { r, g, b } = parseHex(hex);
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

/** WCAG contrast ratio, 1:1 to 21:1. */
export function contrastRatio(a: string, b: string): number {
  const la = luminance(a);
  const lb = luminance(b);
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

export type ContrastUse = 'body-text' | 'large-text' | 'ui-component' | 'decorative';

/**
 * WCAG 2.2 AA thresholds. `ui-component` (1.4.11) covers badges, outlines and focus rings.
 * `decorative` is not a silent skip: a decorative pair must carry a written `why`, and a token
 * in no pair at all fails the build.
 */
export const AA_THRESHOLD: Readonly<Record<ContrastUse, number>> = {
  'body-text': 4.5,
  'large-text': 3,
  'ui-component': 3,
  decorative: 1,
};

export function meetsAA(fg: string, bg: string, use: ContrastUse): boolean {
  return contrastRatio(fg, bg) >= AA_THRESHOLD[use];
}
