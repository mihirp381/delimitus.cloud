import { useEffect, useState } from 'react';

const SEARCH_MS = 250;

export function useSettled(value: string): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), SEARCH_MS);
    return () => window.clearTimeout(timer);
  }, [value]);
  return settled;
}
