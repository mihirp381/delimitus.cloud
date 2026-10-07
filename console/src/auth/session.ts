import type { Refresher, TokenSet } from './oauth';

/** Where the dev token is copied when the user asks to keep it for the tab. */
export const STORAGE_KEY = 'ssc.console.token';
/** How long before the access token expires the session asks for a new one. */
export const REFRESH_EARLY_MS = 60_000;

export interface Session {
  token(): string | null;
  /** Dev login: a pasted token, optionally kept for the tab. */
  set(token: string, keepForTab: boolean): void;
  /** Production sign-in: tokens from the auth host, refreshed before the access token expires. */
  start(tokens: TokenSet): void;
  /** Forgets every token in this tab. */
  clear(): void;
  /** Forgets every token and revokes the sign-in at the auth host. */
  signOut(): Promise<void>;
  /** Called when a refresh fails and the session has ended; one listener. */
  onLost(listener: () => void): void;
}

/** When to refresh: a minute early, but never sooner than halfway through a short lifetime. */
export function refreshDelay(expiresIn: number): number {
  const lifetime = Math.max(0, expiresIn) * 1000;
  return Math.max(lifetime - REFRESH_EARLY_MS, lifetime / 2);
}

/**
 * The API token, held in memory. With `storage` (sessionStorage, dev login only) a pasted token
 * can also be kept for the tab, so a reload does not sign the user out. Tokens from the auth host
 * (`start`) are never written anywhere: the access token is refreshed through `refresher` about a
 * minute before it expires, and a failed refresh ends the session (`onLost`).
 */
export function createSession(storage: Storage | null, refresher: Refresher | null = null): Session {
  let current: string | null = null;
  let refreshToken: string | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  // Bumped on every change, so a refresh that answers after a sign-out or a new sign-in is dropped.
  let generation = 0;
  let lost: () => void = () => undefined;
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
  const reset = () => {
    generation += 1;
    if (timer !== null) clearTimeout(timer);
    timer = null;
    current = null;
    refreshToken = null;
    write(null);
  };
  const begin = (tokens: TokenSet) => {
    reset();
    current = tokens.accessToken;
    refreshToken = tokens.refreshToken;
    if (refresher === null) return;
    const mine = generation;
    timer = setTimeout(() => {
      timer = null;
      refresher.refresh(tokens.refreshToken).then(
        (next) => {
          if (mine === generation) begin(next);
        },
        () => {
          if (mine !== generation) return;
          reset();
          lost();
        },
      );
    }, refreshDelay(tokens.expiresIn));
  };
  return {
    token: () => current,
    set(token, keepForTab) {
      reset();
      current = token;
      write(keepForTab ? token : null);
    },
    start: begin,
    clear: reset,
    async signOut() {
      const held = refreshToken;
      reset();
      if (held === null || refresher === null) return;
      try {
        await refresher.revoke(held);
      } catch {
        // The tab has forgotten the tokens; the auth host ends the session when it expires.
      }
    },
    onLost(listener) {
      lost = listener;
    },
  };
}
