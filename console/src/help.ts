import type { ApiClient } from './api/client';
import { ApiProblem } from './api/problem';

const EMAIL = /^[^@\s<>"',;:?&#%]+@[^@\s<>"',;:?&#%]+\.[^@\s<>"',;:?&#%]+$/;

/**
 * The support address a build was given (`VITE_SSC_SUPPORT_EMAIL`), or null when it has none or
 * what it has is not one plain address: the page then names no address rather than a link that
 * goes nowhere.
 */
export function supportEmail(raw: string | undefined): string | null {
  const value = (raw ?? '').trim();
  return EMAIL.test(value) ? value : null;
}

/** The trust pack's address (`VITE_SSC_TRUST_URL`), or null unless it is an https URL. */
export function trustUrl(raw: string | undefined): string | null {
  const value = (raw ?? '').trim();
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}

/**
 * Whether the API answers right now: one small read on the console's own origin, which is all
 * the page's content security policy lets it reach. Any answer the API itself gives counts, a
 * refusal included; no answer, or a 5xx from it or from what stands in front of it, does not.
 */
export async function sscAnswers(api: ApiClient): Promise<boolean> {
  try {
    await api.GET('/v1/whoami');
    return true;
  } catch (error) {
    return error instanceof ApiProblem && error.status < 500;
  }
}
