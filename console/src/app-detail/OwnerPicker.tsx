import { keepPreviousData, useInfiniteQuery } from '@tanstack/react-query';
import { useRouteContext } from '@tanstack/react-router';
import { useEffect, useId, useState } from 'react';
import { type Match, USER_ID_PATTERN } from '../api/directory';
import { rosterPage } from '../api/people';
import { Button } from '../components/Button';
import { Loading } from '../components/Loading';
import { ProblemNotice } from '../components/ProblemNotice';
import { useSettled } from '../hooks';

interface Props {
  /** Left out of the list: the app's owner now. */
  readonly current: string;
  readonly picked: Match | null;
  readonly onPick: (match: Match | null) => void;
}

/**
 * The org's active people, by name, to pick a new owner from: searched by part of a name or email,
 * a page at a time. A typed `usr_` id is taken as it is; a typed email picks the one active person with it.
 */
export function OwnerPicker({ current, picked, onPick }: Props) {
  const { api } = useRouteContext({ from: '/_authed' });
  const [text, setText] = useState('');
  const inputId = useId();
  const name = useId();
  const typed = text.trim();
  const isId = USER_ID_PATTERN.test(typed);
  const q = useSettled(isId ? '' : typed);
  const list = useInfiniteQuery({
    queryKey: ['users', q, 'active'],
    queryFn: ({ pageParam, signal }) => rosterPage(api, q, 'active', pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
    placeholderData: keepPreviousData,
    enabled: !isId,
  });
  const people = (list.data?.pages.flatMap((p) => p.users) ?? []).filter((u) => u.id !== current);
  const sameEmail = q && q === typed ? people.filter((u) => u.email.toLowerCase() === typed.toLowerCase()) : [];
  const exact = sameEmail.length === 1 && !list.hasNextPage ? sameEmail[0] : undefined;

  useEffect(() => {
    if (exact) onPick({ id: exact.id, label: exact.display_name || exact.email });
  }, [exact, onPick]);

  function change(value: string) {
    setText(value);
    const v = value.trim();
    onPick(USER_ID_PATTERN.test(v) ? { id: v, label: v } : null);
  }

  return (
    <div className="stack">
      <div className="field">
        <label htmlFor={inputId}>New owner</label>
        <input
          id={inputId}
          type="search"
          value={text}
          placeholder="Search by name or email, or type a usr_ id"
          autoComplete="off"
          spellCheck={false}
          maxLength={200}
          onChange={(e) => change(e.target.value)}
        />
      </div>
      {isId ? (
        <p className="muted">The person with this id, if they are active.</p>
      ) : list.isError ? (
        <ProblemNotice error={list.error} />
      ) : !list.data ? (
        <Loading label="Loading people…" />
      ) : people.length === 0 ? (
        <p className="empty">{q ? `No active person matches ${q}.` : 'No other active people.'}</p>
      ) : (
        <>
          {sameEmail.length > 1 ? <p className="muted">Several people share this address. Pick one.</p> : null}
          <fieldset className="choices">
            <legend className="visually-hidden">Active people</legend>
            {people.map((u) => (
              <label key={u.id} className="check">
                <input
                  type="radio"
                  name={name}
                  value={u.id}
                  checked={picked?.id === u.id}
                  onChange={() => onPick({ id: u.id, label: u.display_name || u.email })}
                />
                <span>
                  {u.display_name || u.email}
                  {u.display_name ? <span className="muted"> · {u.email}</span> : null}
                </span>
              </label>
            ))}
          </fieldset>
        </>
      )}
      {!isId && list.hasNextPage ? (
        <Button disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()}>
          {list.isFetchingNextPage ? 'Loading…' : 'Load more people'}
        </Button>
      ) : null}
    </div>
  );
}
