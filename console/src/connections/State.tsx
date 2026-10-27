import type { Connection } from '../api/connections';
import { Badge } from '../components/Badge';

export function ConnectionState({ connection: c }: { readonly connection: Connection }) {
  return (
    <>
      <Badge tone={c.status === 'active' ? 'success' : 'danger'}>{c.status}</Badge>{' '}
      <Badge tone={c.setup_status === 'ready' ? 'success' : 'warning'}>{c.setup_status}</Badge>
    </>
  );
}
