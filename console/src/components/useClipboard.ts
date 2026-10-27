import { useCallback, useEffect, useState } from 'react';

export type ClipboardState = 'idle' | 'copied' | 'failed';

export function useClipboard(resetMs?: number) {
  const [state, setState] = useState<ClipboardState>('idle');
  useEffect(() => {
    if (state === 'idle' || resetMs === undefined) return undefined;
    const timer = window.setTimeout(() => setState('idle'), resetMs);
    return () => window.clearTimeout(timer);
  }, [state, resetMs]);
  const copy = useCallback(async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setState('copied');
    } catch {
      setState('failed');
    }
  }, []);
  return { state, copy };
}

export function copyMessage(state: ClipboardState, what: string): string {
  return state === 'copied' ? 'Copied.' : state === 'failed' ? `Could not copy; select the ${what} instead.` : '';
}
