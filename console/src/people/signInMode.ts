import { useRouteContext } from '@tanstack/react-router';
import type { SignInMode } from '../api/people';

export function useSignInMode(): SignInMode {
  const { queries } = useRouteContext({ from: '/_authed' });
  return queries.useQuery('get', '/v1/whoami').data?.sign_in_mode ?? 'directory';
}
