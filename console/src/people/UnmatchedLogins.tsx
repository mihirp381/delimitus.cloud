import { useRouteContext } from '@tanstack/react-router';
import { type FormEvent, type ReactNode, useState } from 'react';
import { findPeople, type Match, USER_ID_PATTERN } from '../api/directory';
import { linkLogin, NOT_LINKABLE, REASON_TEXT, type UnlinkedLogin } from '../api/people';
import { Lookup } from '../app-detail/Lookup';
import { Ago } from '../components/Ago';
import { Button } from '../components/Button';
import { Dialog } from '../components/Dialog';
import { Loading } from '../components/Loading';
import { Panel } from '../components/Panel';
import { ProblemNotice } from '../components/ProblemNotice';
import { type Column, Table } from '../components/Table';
import { useToast } from '../components/Toast';

function when(iso: string): ReactNode {
  return <Ago at={iso} />;
}

/**
 * The logins no person could be found for (`ssc logins list`), and linking one to an active
 * person (`ssc logins link`). An address-shaped subject cannot be linked: the API says which.
 */
export function UnmatchedLogins() {
  const { queries } = useRouteContext({ from: '/_authed' });
  const list = queries.useQuery('get', '/v1/unlinked-logins');
  const toast = useToast();

  const columns: readonly Column<UnlinkedLogin>[] = [
    { header: 'Email', cell: (u) => u.email },
    { header: 'Why', cell: (u) => REASON_TEXT[u.reason] },
    { header: 'Attempts', cell: (u) => u.attempts },
    { header: 'First seen', cell: (u) => when(u.first_seen_at) },
    { header: 'Last seen', cell: (u) => when(u.last_seen_at) },
    {
      header: 'Linkable',
      className: 'actions-cell',
      cell: (u) =>
        u.linkable ? (
          <LinkLogin
            login={u}
            onDone={(message) => {
              toast(message);
              void list.refetch();
            }}
          />
        ) : (
          <span className="muted">{NOT_LINKABLE}</span>
        ),
    },
  ];

  return (
    <Panel title="Unmatched logins">
      <p className="muted">
        A login lands here when its identity provider subject matches no linked person and its
        email matches no one, or more than one. Link it to an active person, and that
        person&apos;s next login with it signs them in. A login whose subject is an address
        cannot be linked: fix the person in the directory instead.
      </p>
      {list.isPending ? (
        <Loading label="Loading logins…" rows={3} />
      ) : list.isError ? (
        <ProblemNotice error={list.error} />
      ) : (
        <Table
          caption="Unmatched logins, most recent first"
          columns={columns}
          rows={list.data.unlinked_logins}
          rowKey={(u) => u.id}
          empty="Every login so far matched a person."
          emptyHint="A login that matches no one in the directory shows up here to be linked."
        />
      )}
    </Panel>
  );
}

function LinkLogin({ login, onDone }: { readonly login: UnlinkedLogin; readonly onDone: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  const title = `Link ${login.email} to a person`;
  return (
    <>
      <Button onClick={() => setOpen(true)} aria-label={title}>
        Link to a person
      </Button>
      <Dialog open={open} title={title} onClose={() => setOpen(false)}>
        {open ? (
          <LinkForm
            login={login}
            title={title}
            onClose={() => setOpen(false)}
            onDone={(message) => {
              setOpen(false);
              onDone(message);
            }}
          />
        ) : null}
      </Dialog>
    </>
  );
}

interface LinkFormProps {
  readonly login: UnlinkedLogin;
  readonly title: string;
  readonly onClose: () => void;
  readonly onDone: (message: string) => void;
}

/** Picks the person by exact email or `usr_` id: only an active one, as the API requires. */
function LinkForm({ login, title, onClose, onDone }: LinkFormProps) {
  const { api } = useRouteContext({ from: '/_authed' });
  const [person, setPerson] = useState<Match | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!person || busy) return;
    setBusy(true);
    setError(null);
    try {
      const linked = await linkLogin(api, login.id, person.id);
      onDone(`Linked ${login.email} to ${linked.user_name}. Their next login signs them in.`);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="stack" onSubmit={submit} aria-label={title}>
      <p>
        The login <strong>{login.email}</strong> will sign in as the person you pick, with
        everything that person can open. Pick the person it belongs to.
      </p>
      <dl className="facts">
        <dt>Why it matched no one</dt>
        <dd>{REASON_TEXT[login.reason]}</dd>
        <dt>Attempts</dt>
        <dd>{login.attempts}</dd>
        <dt>Subject</dt>
        <dd>
          <code>{login.subject}</code>
        </dd>
      </dl>
      <Lookup
        label="The person: email or usr_ id"
        placeholder="name@example.com"
        idPattern={USER_ID_PATTERN}
        search={(email) => findPeople(api, email)}
        hint="Type the person's whole email address and press Find, or paste a usr_ id. Only an active person can be picked."
        picked={person}
        onPick={(match) => {
          setPerson(match);
          setError(null);
        }}
      />
      {error ? <ProblemNotice error={error} /> : null}
      <div className="actions">
        <Button onClick={onClose}>Cancel</Button>
        <Button type="submit" variant="primary" disabled={!person || busy}>
          {busy ? 'Linking…' : 'Link'}
        </Button>
      </div>
    </form>
  );
}
