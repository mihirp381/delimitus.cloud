/**
 * Layer 1: raw colour scales, copied from the Delimitus UI tokens
 * (Ristretto-python-conversion `frontend/ui/src/tokens/primitives.ts` at ef688e9b).
 * Only the scales the console uses are kept; values are unchanged.
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

/** Confirmed, live. */
export const JADE = {
  '200': '#b6f5da',
  '300': '#7aeabf',
  '400': '#3ddc97',
  '500': '#16b877',
  '600': '#0d8f5b',
  '700': '#0a6b45',
} as const;

export const AMBER = {
  '200': '#ffe9a8',
  '300': '#ffd66b',
  '400': '#f5b301',
  '500': '#cf9200',
  '600': '#9c6d00',
  '700': '#6d4c00',
  '800': '#a35200',
} as const;

/** Stopped, held, refused. */
export const ROSE = {
  '200': '#ffd2ce',
  '300': '#ffada6',
  '400': '#ff7a6e',
  '500': '#e04b3f',
  '600': '#b3261e',
  '700': '#821a14',
} as const;

/** In flight, informational. */
export const AZURE = {
  '200': '#cfe2ff',
  '300': '#a3c7ff',
  '400': '#6aa6ff',
  '500': '#3b82f6',
  '600': '#2563eb',
  '700': '#1a49b8',
} as const;

/** Accent in the dark theme: interactive, focused, selected. */
export const IRIS = {
  '50': '#f5f3ff',
  '100': '#ebe7ff',
  '200': '#ddd8ff',
  '300': '#bcb2ff',
  '400': '#9b8cff',
  '500': '#7c6bff',
  '600': '#5b47f0',
  '700': '#4433c4',
  '950': '#16131f',
} as const;

/** Stale, degraded. */
export const CLAY = {
  '200': '#eddcc9',
  '300': '#d9bf9f',
  '400': '#bf9c72',
  '500': '#9c7a52',
  '600': '#7a5c3b',
  '700': '#573f28',
} as const;

/** Accent in the light theme. */
export const SPECTRUM = {
  blue: '#0071e3',
  blueHover: '#0062c4',
  blueInk: '#0058b0',
  blueSoft: '#e8f1fd',
} as const;

export const PRIMITIVES = { INK, JADE, AMBER, ROSE, AZURE, IRIS, CLAY, SPECTRUM } as const;

/** Every primitive scale name, for the lint that keeps components away from them. */
export const PRIMITIVE_SCALE_NAMES = Object.keys(PRIMITIVES);
