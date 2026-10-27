import { useRouteContext } from '@tanstack/react-router';
import { changeConnection, type ConnectionChange } from '../api/connections';
import { useToast } from '../components/Toast';

export function useRefreshConnections() {
  const { queries, queryClient } = useRouteContext({ from: '/_authed' });
  return () => queryClient.invalidateQueries({ queryKey: queries.queryOptions('get', '/v1/connections').queryKey });
}

export function useConnectionChange(name: string) {
  const { api } = useRouteContext({ from: '/_authed' });
  const refresh = useRefreshConnections();
  const toast = useToast();
  return async (patch: ConnectionChange, message: string) => {
    await changeConnection(api, name, patch);
    await refresh();
    toast(message);
  };
}

export function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}
