/**
 * The theme a person chose: light, dark, or whatever their system says. The choice is kept in
 * localStorage and applied as `data-theme` on the root element, which `tokens.css` reads; with no
 * attribute the system setting decides.
 *
 * `public/theme.js` applies the same choice before the first paint. It is a file of this origin,
 * loaded from the document's head, because the host's policy allows no inline script; a test
 * holds the two to the same key and values.
 */
export const THEME_KEY = 'ssc-console-theme';

export const THEME_CHOICES = ['light', 'dark', 'system'] as const;
export type ThemeChoice = (typeof THEME_CHOICES)[number];

export const THEME_LABEL: Readonly<Record<ThemeChoice, string>> = {
  light: 'Light',
  dark: 'Dark',
  system: 'System',
};

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    // Storage can be refused outright; the choice then lasts for the page.
    return null;
  }
}

export function readTheme(): ThemeChoice {
  const kept = storage()?.getItem(THEME_KEY);
  return kept === 'light' || kept === 'dark' ? kept : 'system';
}

export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', choice);
}

export function chooseTheme(choice: ThemeChoice): void {
  const store = storage();
  try {
    if (choice === 'system') store?.removeItem(THEME_KEY);
    else store?.setItem(THEME_KEY, choice);
  } catch {
    // A full or refused store: the choice still applies to this page.
  }
  applyTheme(choice);
}
