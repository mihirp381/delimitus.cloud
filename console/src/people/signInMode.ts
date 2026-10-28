import { useRouteContext } from '@tanstack/react-router';
import type { SignInMode } from '../api/people';

function useWhoami() {
  const { queries } = useRouteContext({ from: '/_authed' });
  return queries.useQuery('get', '/v1/whoami').data;
}

export function useSignInMode(): SignInMode {
  return useWhoami()?.sign_in_mode ?? 'directory';
}

export function useInviteDomains(): readonly string[] {
  return useWhoami()?.invite_domains ?? [];
}

export function atDomains(email: string, domains: readonly string[]): boolean {
  const at = email.lastIndexOf('@');
  return at >= 0 && domains.includes(email.slice(at + 1).toLowerCase());
}
