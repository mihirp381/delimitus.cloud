/**
 * Layer 2: what each colour is for. Components reference only these, through the `--ssc-*`
 * custom properties in `tokens.css`. The neutral ramp and the layering come from the Delimitus UI
 * tokens (`frontend/ui/src/tokens/semantic.ts` at ef688e9b); the accent, the tones, the state
 * colours, the fonts, the radii and the shadows are the landing page's (`landing/index.html`),
 * with a dark counterpart for each, since the landing ships light only.
 */

import type { ContrastUse } from './contrast';
import {
  INK,
  SIGNAL,
  SIGNAL_NIGHT,
  SPECTRUM,
  SPECTRUM_NIGHT,
  STOPS,
  TOPIC,
  TOPIC_NIGHT,
  TOPIC_NIGHT_SOFT,
  TOPIC_SOFT,
} from './primitives';

export const THEMES = ['light', 'dark'] as const;
export type Theme = (typeof THEMES)[number];

/** A token added here without a value in both themes fails to compile. */
export interface SemanticTokens {
  /* Surfaces, from furthest back to nearest front. The page is `surfaceSunken`; a card on it is
     `surfaceRaised`; a dialog is `surfaceOverlay`. `surfaceSubtle` is a table's heading row and
     `surfaceHover` the row under the pointer. */
  readonly surface: string;
  readonly surfaceSunken: string;
  readonly surfaceSubtle: string;
  readonly surfaceRaised: string;
  readonly surfaceOverlay: string;
  readonly surfaceHover: string;

  /* Lines: one you are not meant to notice, one you are. */
  readonly borderSubtle: string;
  readonly borderStrong: string;

  /* Text. `muted` is for supporting information, never body copy. */
  readonly textPrimary: string;
  readonly textSecondary: string;
  readonly textMuted: string;
  readonly textInverse: string;

  /* Interaction. `accent` is the only colour that means "you may press this"; `accentInk` is the
     same blue deepened for text (links, outlined buttons), as on the landing. */
  readonly accent: string;
  readonly accentHover: string;
  readonly accentContrast: string;
  readonly accentSoft: string;
  readonly accentSoftContrast: string;
  readonly accentLine: string;
  readonly accentInk: string;
  readonly accentInkHover: string;
  readonly focusRing: string;

  /* Feedback about an action or a state: the text colour, the tint behind it, the line round it. */
  readonly feedbackSuccess: string;
  readonly feedbackSuccessSoft: string;
  readonly feedbackSuccessLine: string;
  readonly feedbackWarning: string;
  readonly feedbackWarningSoft: string;
  readonly feedbackWarningLine: string;
  readonly feedbackDanger: string;
  readonly feedbackDangerSoft: string;
  readonly feedbackDangerLine: string;
  readonly feedbackInfo: string;
  readonly feedbackInfoSoft: string;
  readonly feedbackInfoLine: string;

  /* The landing's per-topic tones, for section labels and tinted pills. They name a topic, never
     a state: state is the feedback colours' job. */
  readonly toneBlue: string;
  readonly toneBlueSoft: string;
  readonly toneViolet: string;
  readonly toneVioletSoft: string;
  readonly tonePurple: string;
  readonly tonePurpleSoft: string;
  readonly toneMagenta: string;
  readonly toneMagentaSoft: string;
  readonly toneOrange: string;
  readonly toneOrangeSoft: string;
}

export const LIGHT: SemanticTokens = {
  surface: INK[0],
  surfaceSunken: INK[50],
  surfaceSubtle: INK[25],
  surfaceRaised: INK[0],
  surfaceOverlay: INK[0],
  surfaceHover: INK[50],

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
  accentLine: SPECTRUM.blueLine,
  accentInk: SPECTRUM.blueInk,
  accentInkHover: SPECTRUM.blueHover,
  focusRing: SPECTRUM.blue,

  feedbackSuccess: SIGNAL.live,
  feedbackSuccessSoft: SIGNAL.liveSoft,
  feedbackSuccessLine: SIGNAL.liveLine,
  feedbackWarning: SIGNAL.caution,
  feedbackWarningSoft: SIGNAL.cautionSoft,
  feedbackWarningLine: SIGNAL.cautionLine,
  feedbackDanger: SIGNAL.danger,
  feedbackDangerSoft: SIGNAL.dangerSoft,
  feedbackDangerLine: SIGNAL.dangerLine,
  feedbackInfo: TOPIC.blue,
  feedbackInfoSoft: TOPIC_SOFT.blue,
  feedbackInfoLine: SPECTRUM.blueLine,

  toneBlue: TOPIC.blue,
  toneBlueSoft: TOPIC_SOFT.blue,
  toneViolet: TOPIC.violet,
  toneVioletSoft: TOPIC_SOFT.violet,
  tonePurple: TOPIC.purple,
  tonePurpleSoft: TOPIC_SOFT.purple,
  toneMagenta: TOPIC.magenta,
  toneMagentaSoft: TOPIC_SOFT.magenta,
  toneOrange: TOPIC.orange,
  toneOrangeSoft: TOPIC_SOFT.orange,
};

export const DARK: SemanticTokens = {
  surface: INK[950],
  surfaceSunken: INK[1000],
  surfaceSubtle: INK[950],
  surfaceRaised: INK[900],
  surfaceOverlay: INK[800],
  surfaceHover: INK[800],

  borderSubtle: INK[800],
  borderStrong: INK[400],

  textPrimary: INK[50],
  textSecondary: INK[300],
  textMuted: INK[400],
  textInverse: INK[1000],

  accent: SPECTRUM_NIGHT.blue,
  accentHover: SPECTRUM_NIGHT.blueHover,
  accentContrast: INK[1000],
  accentSoft: SPECTRUM_NIGHT.blueSoft,
  accentSoftContrast: SPECTRUM_NIGHT.blueInkHover,
  accentLine: SPECTRUM_NIGHT.blueLine,
  accentInk: SPECTRUM_NIGHT.blueInk,
  accentInkHover: SPECTRUM_NIGHT.blueInkHover,
  focusRing: SPECTRUM_NIGHT.blue,

  feedbackSuccess: SIGNAL_NIGHT.live,
  feedbackSuccessSoft: SIGNAL_NIGHT.liveSoft,
  feedbackSuccessLine: SIGNAL_NIGHT.liveLine,
  feedbackWarning: SIGNAL_NIGHT.caution,
  feedbackWarningSoft: SIGNAL_NIGHT.cautionSoft,
  feedbackWarningLine: SIGNAL_NIGHT.cautionLine,
  feedbackDanger: SIGNAL_NIGHT.danger,
  feedbackDangerSoft: SIGNAL_NIGHT.dangerSoft,
  feedbackDangerLine: SIGNAL_NIGHT.dangerLine,
  feedbackInfo: TOPIC_NIGHT.blue,
  feedbackInfoSoft: TOPIC_NIGHT_SOFT.blue,
  feedbackInfoLine: SPECTRUM_NIGHT.blueLine,

  toneBlue: TOPIC_NIGHT.blue,
  toneBlueSoft: TOPIC_NIGHT_SOFT.blue,
  toneViolet: TOPIC_NIGHT.violet,
  toneVioletSoft: TOPIC_NIGHT_SOFT.violet,
  tonePurple: TOPIC_NIGHT.purple,
  tonePurpleSoft: TOPIC_NIGHT_SOFT.purple,
  toneMagenta: TOPIC_NIGHT.magenta,
  toneMagentaSoft: TOPIC_NIGHT_SOFT.magenta,
  toneOrange: TOPIC_NIGHT.orange,
  toneOrangeSoft: TOPIC_NIGHT_SOFT.orange,
};

export const THEME_TOKENS: Readonly<Record<Theme, SemanticTokens>> = { light: LIGHT, dark: DARK };

/** Both themes ship; the console follows the system setting. */
export const SHIPPING_THEMES: readonly Theme[] = THEMES;

/* Tokens that are not colours, the same in both themes. */

/** The landing's system stacks: no web font is loaded. */
export const FONT_FAMILIES = {
  sans: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', Helvetica, Arial, sans-serif",
  display:
    "-apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Helvetica Neue', Helvetica, Arial, sans-serif",
  mono: "ui-monospace, 'SF Mono', Menlo, Consolas, monospace",
} as const;

/**
 * Type: the landing's 17px body with its line height and tight tracking. Tables and controls sit
 * a step down, since a console row carries more than a landing paragraph does.
 */
export const TYPE = {
  sizeBody: '17px',
  sizeControl: '15px',
  sizeTable: '15px',
  sizeSmall: '14px',
  sizeMicro: '12px',
  /* A page's `h1`, a card's `h2` and a section label, after the landing's `.h2`, `.app__title`
     and `.label`. */
  sizeTitle: 'clamp(1.9rem, 4vw, 2.6rem)',
  sizeHeading: '1.4rem',
  sizeLabel: '1rem',
  leadingBody: '1.52',
  leadingTight: '1.2',
  leadingDisplay: '1.07',
  trackingBody: '-0.012em',
  trackingLabel: '-0.01em',
  trackingHeading: '-0.025em',
  trackingDisplay: '-0.03em',
} as const;

/** The landing's radii; `xl` is its card. */
export const RADIUS = { sm: '6px', md: '12px', lg: '18px', xl: '24px', full: '999px' } as const;

/** The one spacing scale. Every gap, padding and margin in `styles.css` is a step of it. */
export const SPACE = {
  '0': '2px',
  '1': '4px',
  '2': '8px',
  '3': '12px',
  '4': '16px',
  '5': '24px',
  '6': '32px',
  '7': '48px',
  '8': '72px',
} as const;

/** Page measures: the landing's content width and nav height. */
export const LAYOUT = { pageMax: '1200px', barHeight: '57px' } as const;

export const MOTION = { ease: 'cubic-bezier(0.16, 1, 0.3, 1)' } as const;

/**
 * The landing's `--spectrum`, the one brand mark: the rule under the top bar and across the top
 * of the sign-in card, and nowhere else.
 */
export const GRADIENTS = {
  spectrum: `linear-gradient(90deg, ${STOPS.blue} 0%, ${STOPS.violet} 22%, ${STOPS.purple} 44%, ${STOPS.magenta} 64%, ${STOPS.coral} 82%, ${STOPS.orange} 100%)`,
} as const;

/**
 * Shadows, the dialog backdrop and the translucent top bar, per theme. The light shadows are the
 * landing's `--shadow-soft` and `--shadow-lift`; on dark surfaces a shadow reads poorly, so a
 * hairline of light along the top edge does the lifting.
 */
export const EFFECTS: Readonly<
  Record<Theme, Readonly<Record<'shadowSoft' | 'shadowLift' | 'scrim' | 'barSurface', string>>>
> = {
  light: {
    shadowSoft: '0 1px 2px rgb(0 0 0 / 0.04), 0 12px 32px -12px rgb(0 0 0 / 0.10)',
    shadowLift: '0 2px 6px rgb(0 0 0 / 0.04), 0 34px 70px -34px rgb(29 29 31 / 0.28)',
    scrim: 'rgb(16 16 21 / 0.40)',
    barSurface: 'rgb(255 255 255 / 0.72)',
  },
  dark: {
    shadowSoft: 'inset 0 1px 0 rgb(255 255 255 / 0.05), 0 12px 32px -12px rgb(0 0 0 / 0.60)',
    shadowLift: 'inset 0 1px 0 rgb(255 255 255 / 0.07), 0 2px 6px rgb(0 0 0 / 0.40), 0 34px 70px -34px rgb(0 0 0 / 0.80)',
    scrim: 'rgb(0 0 0 / 0.60)',
    barSurface: 'rgb(22 22 23 / 0.72)',
  },
};

type Pair = {
  readonly fg: keyof SemanticTokens;
  readonly bg: keyof SemanticTokens;
  readonly use: ContrastUse;
  /** Required when `use` is `decorative`. */
  readonly why?: string;
};

/** Where text sits: the page, a card, a dialog, a table's heading row, the row under the pointer. */
export const TEXT_SURFACES = [
  'surface',
  'surfaceSunken',
  'surfaceSubtle',
  'surfaceRaised',
  'surfaceOverlay',
  'surfaceHover',
] as const satisfies readonly (keyof SemanticTokens)[];

export const TONES = ['Blue', 'Violet', 'Purple', 'Magenta', 'Orange'] as const;
export const FEEDBACK = ['Success', 'Warning', 'Danger', 'Info'] as const;

const onEverySurface = (fg: keyof SemanticTokens): Pair[] =>
  TEXT_SURFACES.map((bg) => ({ fg, bg, use: 'body-text' }));

/**
 * Pairs that must clear WCAG AA in every shipping theme. A token in no pair fails the
 * completeness test, so the list cannot quietly stop covering the palette.
 */
export const CONTRAST_PAIRS: readonly Pair[] = [
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

  /* The design pass (GA-10.18). The page is now `surfaceSunken`, a table's heading row
     `surfaceSubtle` and a hovered row `surfaceHover`, so text is checked on those too. */
  ...onEverySurface('textPrimary'),
  ...onEverySurface('textSecondary'),
  ...onEverySurface('textMuted'),
  /* Links and outlined buttons. */
  ...onEverySurface('accentInk'),
  ...onEverySurface('accentInkHover'),
  { fg: 'accentInk', bg: 'accentSoft', use: 'body-text' },
  /* The primary button and the focus ring against the page. */
  { fg: 'accent', bg: 'surfaceSunken', use: 'ui-component' },
  { fg: 'accentHover', bg: 'surfaceSunken', use: 'ui-component' },
  { fg: 'accent', bg: 'surfaceOverlay', use: 'ui-component' },
  { fg: 'focusRing', bg: 'surfaceSubtle', use: 'ui-component' },
  { fg: 'focusRing', bg: 'surfaceHover', use: 'ui-component' },
  { fg: 'borderStrong', bg: 'surfaceSunken', use: 'ui-component' },
  /* The current page in the top bar: `accentSoftContrast` on `accentSoft`, checked above. */
  {
    fg: 'accentLine',
    bg: 'accentSoft',
    use: 'decorative',
    why: 'The edge of a tinted pill whose text and tint already tell it from the surface behind.',
  },
  /* Status pills and notices: the state colour on its own tint, on the page and on every surface
     a pill can sit on; a notice's supporting line is `textSecondary` on the tint. */
  ...FEEDBACK.flatMap((f): Pair[] => [
    ...onEverySurface(`feedback${f}`),
    { fg: `feedback${f}`, bg: `feedback${f}Soft`, use: 'body-text' },
    { fg: 'textPrimary', bg: `feedback${f}Soft`, use: 'body-text' },
    { fg: 'textSecondary', bg: `feedback${f}Soft`, use: 'body-text' },
    {
      fg: `feedback${f}Line`,
      bg: `feedback${f}Soft`,
      use: 'decorative',
      why: 'The edge of a tinted pill or notice; the text inside carries the state and is checked on the tint.',
    },
  ]),
  /* The landing's tones: a section label on any surface, and a tinted pill's text on its tint. */
  ...TONES.flatMap((t): Pair[] => [
    ...onEverySurface(`tone${t}`),
    { fg: `tone${t}`, bg: `tone${t}Soft`, use: 'body-text' },
  ]),
];
