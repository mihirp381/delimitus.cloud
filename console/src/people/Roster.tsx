import { keepPreviousData, useInfiniteQuery } from '@tanstack/react-query';
import { Link, useRouteContext } from '@tanstack/react-router';
import { useEffect, useId, useState } from 'react';
import { type RosterFilter, rosterPage, type UserMatch } from '../api/people';
import { Ago } from '../components/Ago';
import { Button } from '../components/Button';
import { Dialog } from '../components/Dialog';
import { EmptyState } from '../components/EmptyState';
import { Loading } from '../components/Loading';
import { ProblemNotice } from '../components/ProblemNotice';
import { Segmented } from '../components/Segmented';
import { type Column, Table } from '../components/Table';
import { PersonDetail, RoleBadge, StateBadge } from './PersonDetail';

const SEARCH_MS = 250;
const FILTERS: readonly { readonly value: RosterFilter; readonly label: string }[] = [
  { value: 'active', label: 'Active' },
  { value: 'all', label: 'All' },
];

function useSettled(value: string): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), SEARCH_MS);
    return () => window.clearTimeout(timer);
  }, [value]);
  return settled;
}

function columns(open: (p: UserMatch) => void): readonly Column<UserMatch>[] {
  return [
    {
      header: 'Name',
      cell: (p) => (
        <button type="button" className="link-btn" aria-haspopup="dialog" onClick={() => open(p)}>
          {p.display_name || p.email}
        </button>
      ),
    },
    { header: 'Email', cell: (p) => p.email },
    { header: 'Role', cell: (p) => <RoleBadge role={p.role} /> },
    { header: 'State', cell: (p) => <StateBadge status={p.status} /> },
    {
      header: 'Last sign-in',
      className: 'nowrap',
      cell: (p) => (p.last_sign_in_at ? <Ago at={p.last_sign_in_at} /> : <span className="muted">Never</span>),
    },
  ];
}

export function Roster() {
  const { api } = useRouteContext({ from: '/_authed' });
  const [text, setText] = useState('');
  const [filter, setFilter] = useState<RosterFilter>('active');
  const [open, setOpen] = useState<UserMatch | null>(null);
  const searchId = useId();
  const q = useSettled(text.trim());
  const list = useInfiniteQuery({
    queryKey: ['users', q, filter],
    queryFn: ({ pageParam, signal }) => rosterPage(api, q, filter, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
    placeholderData: keepPreviousData,
  });
  const rows = list.data?.pages.flatMap((p) => p.users) ?? [];

  return (
    <>
      <div className="search">
        <label htmlFor={searchId} className="visually-hidden">
          Search people
        </label>
        <input
          id={searchId}
          type="search"
          placeholder="Search by name or email"
          value={text}
          maxLength={200}
          onChange={(e) => setText(e.target.value)}
        />
        <Segmented label="Shown" options={FILTERS} value={filter} onChange={setFilter} />
      </div>
      <section className="panel" aria-label="People">
        {list.data ? (
          rows.length === 0 && !q ? (
            <EmptyState
              hint={
                <>
                  People appear here after the directory sync. <Link to="/help">Help</Link>
                </>
              }
            >
              {filter === 'active' ? 'No active people yet.' : 'No people yet.'}
            </EmptyState>
          ) : (
            <>
              <Table
                caption="People, by name"
                columns={columns(setOpen)}
                rows={rows}
                rowKey={(p) => p.id}
                onOpen={setOpen}
                empty={`No one matches ${q}.`}
                emptyHint={filter === 'active' ? 'Choose All to include deactivated people.' : undefined}
              />
              <div className="more">
                <p className="muted count" aria-live="polite">
                  {rows.length} {rows.length === 1 ? 'person' : 'people'}
                  {list.hasNextPage ? ', more follow' : ''}
                </p>
                {list.hasNextPage ? (
                  <Button disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()}>
                    {list.isFetchingNextPage ? 'Loading…' : 'Load more people'}
                  </Button>
                ) : null}
              </div>
            </>
          )
        ) : list.isError ? (
          <ProblemNotice error={list.error} />
        ) : (
          <Loading label="Loading people…" rows={3} />
        )}
        {list.data && list.isError ? <ProblemNotice error={list.error} /> : null}
      </section>
      <Dialog
        open={open !== null}
        title={open ? open.display_name || open.email : 'Person'}
        onClose={() => setOpen(null)}
      >
        {open ? <PersonDetail person={open} onClose={() => setOpen(null)} /> : null}
      </Dialog>
    </>
  );
}
