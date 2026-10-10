/**
 * Layer 1: raw colour scales. The neutral ramp is copied from the Delimitus UI tokens
 * (Ristretto-python-conversion `frontend/ui/src/tokens/primitives.ts` at ef688e9b); the rest are
 * the landing page's colours (`landing/index.html`, `:root`), so the console and delimitus.com
 * read as one product. The landing is light only: each `*_NIGHT` scale is the same hue taken to
 * a lighter step for text on the dark surfaces, with a deep tint for its soft background.
 *
 * No component may reference this file. `test/tokens.test.ts` scans the source to enforce it,
 * and this is the only place a hex literal is allowed.
 */

/** Neutral ramp with a slight violet cast. */
export const INK = {
  '0': '#ffffff',
  '25': '#fbfbfd',
  '50': '#f5f5f7',
  '100': '#ececf0',
  '200': '#e3e3e8',
  '300': '#d2d2d7',
  '400': '#aeaeb2',
  '500': '#86868b',
  '600': '#6e6e73',
  '700': '#424245',
  '800': '#2c2c2e',
  '900': '#1d1d1f',
  '950': '#161617',
  '1000': '#0b0b0c',
} as const;

/** The one action blue: `--accent`, `--accent-hi`, `--accent-ink`, `--accent-soft`, `--accent-line`. */
export const SPECTRUM = {
  blue: '#0071e3',
  blueHover: '#0062c4',
  blueInk: '#0066cc',
  blueSoft: '#e8f1fd',
  blueLine: '#b9d5f8',
} as const;

/** The action blue on dark surfaces. */
export const SPECTRUM_NIGHT = {
  blue: '#2997ff',
  blueHover: '#5eb0ff',
  blueInk: '#6eb6ff',
  blueInkHover: '#8cc4ff',
  blueSoft: '#0f2742',
  blueLine: '#1d4a7a',
} as const;

/** The stops of the landing's `--spectrum`, the one brand mark. */
export const STOPS = {
  blue: '#0a84ff',
  violet: '#6e5bff',
  purple: '#c24bf0',
  magenta: '#ff378c',
  coral: '#ff5e3a',
  orange: '#ff9f0a',
} as const;

/** The landing's per-topic tones (`--t-*`): the spectrum deepened until text holds 4.5:1. */
export const TOPIC = {
  blue: '#0066cc',
  violet: '#5b3fe0',
  purple: '#a32bc7',
  magenta: '#c8246b',
  orange: '#c4400c',
} as const;

/** The tint behind each tone (`--blue-soft` and the rest). */
export const TOPIC_SOFT = {
  blue: '#e8f1fd',
  violet: '#f0edff',
  purple: '#f8ecfc',
  magenta: '#fdedf3',
  orange: '#fff0e6',
} as const;

export const TOPIC_NIGHT = {
  blue: '#6eb6ff',
  violet: '#a99bff',
  purple: '#da8ff5',
  magenta: '#ff86b3',
  orange: '#ff9a66',
} as const;

export const TOPIC_NIGHT_SOFT = {
  blue: '#0f2742',
  violet: '#1f1a45',
  purple: '#2e1839',
  magenta: '#3a1425',
  orange: '#3a1b0c',
} as const;

/** State colours, the landing's chips: live, caution, danger, each with its tint and its line. */
export const SIGNAL = {
  live: '#147a38',
  liveSoft: '#e6f5ea',
  liveLine: '#b5e0c2',
  caution: '#a35200',
  cautionSoft: '#fff3e0',
  cautionLine: '#f5cf98',
  danger: '#d70015',
  dangerSoft: '#fdecee',
  dangerLine: '#f6bcc3',
} as const;

export const SIGNAL_NIGHT = {
  live: '#4fd27a',
  liveSoft: '#102a19',
  liveLine: '#1f4a2d',
  caution: '#ffb454',
  cautionSoft: '#33230b',
  cautionLine: '#5c4012',
  danger: '#ff7a85',
  dangerSoft: '#3a1217',
  dangerLine: '#6b1f28',
} as const;

export const PRIMITIVES = {
  INK,
  SPECTRUM,
  SPECTRUM_NIGHT,
  STOPS,
  TOPIC,
  TOPIC_SOFT,
  TOPIC_NIGHT,
  TOPIC_NIGHT_SOFT,
  SIGNAL,
  SIGNAL_NIGHT,
} as const;

/** Every primitive scale name, for the lint that keeps components away from them. */
export const PRIMITIVE_SCALE_NAMES = Object.keys(PRIMITIVES);
