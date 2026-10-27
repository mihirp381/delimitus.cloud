import { useRouteContext } from '@tanstack/react-router';
import type { ReactNode } from 'react';
import { Loading } from './Loading';
import { PageHeader } from './PageHeader';
import { ProblemNotice } from './ProblemNotice';

type Props =
  | { readonly title: string; readonly refusal: string; readonly children: ReactNode }
  | { readonly quiet: true; readonly children: ReactNode };

export function AdminOnly(props: Props) {
  const { queries, isAdmin } = useRouteContext({ from: '/_authed' });
  const me = queries.useQuery('get', '/v1/whoami');
  if (isAdmin(me.data)) return props.children;
  if ('quiet' in props) return null;
  const { title, refusal } = props;
  return (
    <>
      <PageHeader title={title} />
      {me.isPending ? (
        <Loading label="Loading…" rows={2} />
      ) : me.isError ? (
        <ProblemNotice error={me.error} />
      ) : (
        <p className="muted">{refusal}</p>
      )}
    </>
  );
}
