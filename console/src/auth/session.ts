/** Where the dev token is copied when the user asks to keep it for the tab. */
export const STORAGE_KEY = 'ssc.console.token';

export interface Session {
  token(): string | null;
  set(token: string, keepForTab: boolean): void;
  clear(): void;
}

/**
 * The API token, held in memory. With `storage` (sessionStorage, dev login only) the token can
 * also be kept for the tab, so a reload does not sign the user out.
 */
export function createSession(storage: Storage | null): Session {
  let current: string | null = null;
  try {
    current = storage?.getItem(STORAGE_KEY) ?? null;
  } catch {
    current = null;
  }
  const write = (value: string | null) => {
    try {
      if (value === null) storage?.removeItem(STORAGE_KEY);
      else storage?.setItem(STORAGE_KEY, value);
    } catch {
      // Storage can be blocked; the in-memory token still works.
    }
  };
  return {
    token: () => current,
    set(token, keepForTab) {
      current = token;
      write(keepForTab ? token : null);
    },
    clear() {
      current = null;
      write(null);
    },
  };
}
