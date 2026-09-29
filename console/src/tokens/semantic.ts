/**
 * Layer 2: what each colour is for. Components reference only these, through the `--ssc-*`
 * custom properties in `tokens.css`. Adapted from the Delimitus UI tokens
 * (`frontend/ui/src/tokens/semantic.ts` at ef688e9b): the pane, brand and synthetic tokens are
 * left out, and the values of the tokens kept are unchanged.
 */

import type { ContrastUse } from './contrast';
import { AMBER, AZURE, CLAY, INK, IRIS, JADE, ROSE, SPECTRUM } from './primitives';

export const THEMES = ['light', 'dark'] as const;
export type Theme = (typeof THEMES)[number];

/** A token added here without a value in both themes fails to compile. */
export interface SemanticTokens {
  /* Surfaces, from furthest back to nearest front. */
  readonly surface: string;
  readonly surfaceSunken: string;
  readonly surfaceRaised: string;
  readonly surfaceOverlay: string;

  /* Lines: one you are not meant to notice, one you are. */
  readonly borderSubtle: string;
  readonly borderStrong: string;

  /* Text. `muted` is for supporting information, never body copy. */
  readonly textPrimary: string;
  readonly textSecondary: string;
  readonly textMuted: string;
  readonly textInverse: string;

  /* Interaction. `accent` is the only colour that means "you may press this". */
  readonly accent: string;
  readonly accentHover: string;
  readonly accentContrast: string;
  readonly accentSoft: string;
  readonly accentSoftContrast: string;
  readonly focusRing: string;

  /* Feedback about an action or a state. */
  readonly feedbackSuccess: string;
  readonly feedbackWarning: string;
  readonly feedbackDanger: string;
  readonly feedbackInfo: string;
}

export const LIGHT: SemanticTokens = {
  surface: INK[0],
  surfaceSunken: INK[50],
  surfaceRaised: INK[0],
  surfaceOverlay: INK[0],

  borderSubtle: INK[200],
  borderStrong: INK[500],

  textPrimary: INK[900],
  textSecondary: INK[700],
  textMuted: INK[600],
  textInverse: INK[0],

  accent: SPECTRUM.blue,
  accentHover: SPECTRUM.blueHover,
  accentContrast: INK[0],
  accentSoft: SPECTRUM.blueSoft,
  accentSoftContrast: SPECTRUM.blueInk,
  focusRing: SPECTRUM.blue,

  feedbackSuccess: JADE[700],
  feedbackWarning: AMBER[800],
  feedbackDanger: ROSE[600],
  feedbackInfo: AZURE[700],
};

export const DARK: SemanticTokens = {
  surface: INK[950],
  surfaceSunken: INK[1000],
  surfaceRaised: INK[900],
  surfaceOverlay: INK[800],

  borderSubtle: INK[800],
  borderStrong: INK[400],

  textPrimary: INK[50],
  textSecondary: INK[300],
  textMuted: INK[400],
  textInverse: INK[1000],

  accent: IRIS[400],
  accentHover: IRIS[300],
  accentContrast: INK[1000],
  accentSoft: IRIS[950],
  accentSoftContrast: IRIS[300],
  focusRing: IRIS[400],

  feedbackSuccess: JADE[400],
  feedbackWarning: CLAY[300],
  feedbackDanger: ROSE[400],
  feedbackInfo: AZURE[400],
};

export const THEME_TOKENS: Readonly<Record<Theme, SemanticTokens>> = { light: LIGHT, dark: DARK };

/** Both themes ship; the console follows the system setting. */
export const SHIPPING_THEMES: readonly Theme[] = THEMES;

/** Tokens that are not colours, the same in both themes. */
export const FONT_FAMILIES = {
  ui: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Inter Variable', system-ui, 'Segoe UI', Roboto, sans-serif",
  mono: "'JetBrains Mono Variable', ui-monospace, 'SF Mono', Menlo, Consolas, monospace",
} as const;

export const RADIUS = { sm: '6px', md: '10px', lg: '16px', full: '999px' } as const;

/** Shadows and the dialog backdrop, per theme. */
export const EFFECTS: Readonly<Record<Theme, Readonly<Record<'shadowRaised' | 'shadowOverlay' | 'scrim', string>>>> = {
  light: {
    shadowRaised: '0 1px 2px rgb(0 0 0 / 0.04), 0 8px 24px -12px rgb(0 0 0 / 0.10)',
    shadowOverlay: '0 2px 6px rgb(0 0 0 / 0.04), 0 24px 60px -24px rgb(29 29 31 / 0.24)',
    scrim: 'rgb(16 16 21 / 0.40)',
  },
  dark: {
    shadowRaised: '0 1px 0 rgb(255 255 255 / 0.04)',
    shadowOverlay: '0 8px 24px rgb(0 0 0 / 0.55), 0 1px 0 rgb(255 255 255 / 0.06)',
    scrim: 'rgb(0 0 0 / 0.60)',
  },
};

/**
 * Pairs that must clear WCAG AA in every shipping theme. A token in no pair fails the
 * completeness test, so the list cannot quietly stop covering the palette.
 */
export const CONTRAST_PAIRS: readonly {
  readonly fg: keyof SemanticTokens;
  readonly bg: keyof SemanticTokens;
  readonly use: ContrastUse;
  /** Required when `use` is `decorative`. */
  readonly why?: string;
}[] = [
  {
    fg: 'borderSubtle',
    bg: 'surface',
    use: 'decorative',
    why: 'A hairline between table rows, which are identified by position. Anything that must be seen to be used, such as an input outline, takes borderStrong, which is checked.',
  },
  { fg: 'textPrimary', bg: 'surface', use: 'body-text' },
  { fg: 'textPrimary', bg: 'surfaceSunken', use: 'body-text' },
  { fg: 'textPrimary', bg: 'surfaceRaised', use: 'body-text' },
  { fg: 'textPrimary', bg: 'surfaceOverlay', use: 'body-text' },
  { fg: 'textSecondary', bg: 'surface', use: 'body-text' },
  { fg: 'textSecondary', bg: 'surfaceSunken', use: 'body-text' },
  { fg: 'textSecondary', bg: 'surfaceRaised', use: 'body-text' },
  { fg: 'textSecondary', bg: 'surfaceOverlay', use: 'body-text' },
  { fg: 'textMuted', bg: 'surface', use: 'body-text' },
  { fg: 'textMuted', bg: 'surfaceRaised', use: 'body-text' },
  { fg: 'textMuted', bg: 'surfaceOverlay', use: 'body-text' },
  { fg: 'accent', bg: 'surface', use: 'body-text' },
  { fg: 'accent', bg: 'surfaceRaised', use: 'body-text' },
  { fg: 'accentHover', bg: 'surface', use: 'body-text' },
  { fg: 'accentHover', bg: 'surfaceRaised', use: 'body-text' },
  { fg: 'accentContrast', bg: 'accent', use: 'body-text' },
  { fg: 'textInverse', bg: 'accent', use: 'body-text' },
  { fg: 'accentContrast', bg: 'accentHover', use: 'body-text' },
  { fg: 'accentSoftContrast', bg: 'accentSoft', use: 'body-text' },
  { fg: 'focusRing', bg: 'surface', use: 'ui-component' },
  { fg: 'focusRing', bg: 'surfaceSunken', use: 'ui-component' },
  { fg: 'focusRing', bg: 'surfaceRaised', use: 'ui-component' },
  { fg: 'focusRing', bg: 'surfaceOverlay', use: 'ui-component' },
  { fg: 'borderStrong', bg: 'surface', use: 'ui-component' },
  { fg: 'borderStrong', bg: 'surfaceRaised', use: 'ui-component' },
  { fg: 'borderStrong', bg: 'surfaceOverlay', use: 'ui-component' },
  { fg: 'feedbackSuccess', bg: 'surfaceRaised', use: 'body-text' },
  { fg: 'feedbackWarning', bg: 'surfaceRaised', use: 'body-text' },
  { fg: 'feedbackDanger', bg: 'surfaceRaised', use: 'body-text' },
  { fg: 'feedbackDanger', bg: 'surfaceOverlay', use: 'body-text' },
  { fg: 'feedbackInfo', bg: 'surfaceRaised', use: 'body-text' },
  { fg: 'feedbackSuccess', bg: 'surfaceOverlay', use: 'body-text' },
  { fg: 'feedbackInfo', bg: 'surfaceOverlay', use: 'body-text' },
  { fg: 'feedbackSuccess', bg: 'surface', use: 'body-text' },
  { fg: 'feedbackWarning', bg: 'surface', use: 'body-text' },
  { fg: 'feedbackDanger', bg: 'surface', use: 'body-text' },
  { fg: 'feedbackInfo', bg: 'surface', use: 'body-text' },
];
