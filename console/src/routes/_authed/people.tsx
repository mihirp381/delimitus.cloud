import { createFileRoute } from '@tanstack/react-router';
import { type FormEvent, type ReactNode, useEffect, useId, useRef, useState } from 'react';
import { findPeople, type Match, USER_ID_PATTERN } from '../../api/directory';
import {
  type GroupMatch,
  groupsByName,
  linkLogin,
  NOT_LINKABLE,
  peopleByEmail,
  REASON_TEXT,
  type UnlinkedLogin,
  type UserMatch,
} from '../../api/people';
import { Lookup } from '../../app-detail/Lookup';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { Dialog } from '../../components/Dialog';
import { PageHeader } from '../../components/PageHeader';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';

export const Route = createFileRoute('/_authed/people')({
  component: PeoplePage,
});

function PeoplePage() {
  const { queries, isAdmin } = Route.useRouteContext();
  const me = queries.useQuery('get', '/v1/whoami');
  if (isAdmin(me.data)) {
    return <People />;
  }
  return (
    <>
      <PageHeader title="People" />
      {me.isPending ? (
        <p className="muted">Loading…</p>
      ) : me.isError ? (
        <ProblemNotice error={me.error} />
      ) : (
        <p className="muted">Only org admins can look people up and link logins.</p>
      )}
    </>
  );
}

function People() {
  return (
    <>
      <PageHeader
        title="People"
        purpose={
          <>
            People and groups are managed in your identity provider: add, change or remove them
            there, and SSC follows. Here you can look one up, and link a login that matched no
            person.
          </>
        }
      />
      <UnmatchedLogins />
      <FindPerson />
      <FindGroup />
    </>
  );
}

function when(iso: string): ReactNode {
  return <time dateTime={iso}>{new Date(iso).toLocaleString()}</time>;
}

/**
 * The logins no person could be found for (`ssc logins list`), and linking one to an active
 * person (`ssc logins link`). An address-shaped subject cannot be linked: the API says which.
 */
function UnmatchedLogins() {
  const { queries } = Route.useRouteContext();
  const list = queries.useQuery('get', '/v1/unlinked-logins');
  const [notice, setNotice] = useState<string | null>(null);
  const headingId = useId();

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
              setNotice(message);
              void list.refetch();
            }}
          />
        ) : (
          <span className="muted">{NOT_LINKABLE}</span>
        ),
    },
  ];

  return (
    <section className="panel" aria-labelledby={headingId}>
      <h2 id={headingId}>Unmatched logins</h2>
      <p className="muted">
        A login lands here when its identity provider subject matches no linked person and its
        email matches no one, or more than one. Link it to an active person, and that
        person&apos;s next login with it signs them in. A login whose subject is an address
        cannot be linked: fix the person in the directory instead.
      </p>
      {notice ? (
        <p className="notice notice-success" role="status">
          {notice}
        </p>
      ) : null}
      {list.isPending ? (
        <p className="muted">Loading logins…</p>
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
    </section>
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
  const { api } = Route.useRouteContext();
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
      const who = person.label === linked.user_id ? linked.user_id : `${person.label} (${linked.user_id})`;
      onDone(`Linked ${login.email} to ${who}. Their next login signs them in.`);
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

interface FinderProps<T> {
  readonly title: string;
  readonly label: string;
  readonly placeholder: string;
  readonly inputType?: 'email' | 'text';
  readonly find: (text: string) => Promise<readonly T[]>;
  readonly caption: (text: string) => string;
  readonly columns: readonly Column<T>[];
  readonly rowKey: (row: T) => string;
  readonly none: (text: string) => string;
  readonly noneHint: string;
  readonly children: ReactNode;
}

/**
 * Looks one thing up by its exact email or name and lists what matched. Read-only. An answer
 * that lands after the text changed, or after the page was left, is dropped.
 */
function Finder<T>(props: FinderProps<T>) {
  const { title, label, placeholder, inputType, find, caption, columns, rowKey, none, noneHint, children } = props;
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [found, setFound] = useState<{ readonly asked: string; readonly rows: readonly T[] } | null>(null);
  const latest = useRef(0);
  const headingId = useId();
  const inputId = useId();
  const typed = text.trim();

  useEffect(
    () => () => {
      latest.current += 1;
    },
    [],
  );

  function change(value: string) {
    latest.current += 1;
    setText(value);
    setFound(null);
    setError(null);
    setBusy(false);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!typed || busy) return;
    const request = ++latest.current;
    setBusy(true);
    setError(null);
    try {
      const rows = await find(typed);
      if (request === latest.current) setFound({ asked: typed, rows });
    } catch (e) {
      if (request === latest.current) setError(e);
    } finally {
      if (request === latest.current) setBusy(false);
    }
  }

  return (
    <section className="panel" aria-labelledby={headingId}>
      <h2 id={headingId}>{title}</h2>
      <p className="muted">{children}</p>
      <form className="lookup-form" onSubmit={submit} aria-label={title}>
        <label className="field" htmlFor={inputId}>
          <span>{label}</span>
          <input
            id={inputId}
            type={inputType ?? 'text'}
            value={text}
            placeholder={placeholder}
            autoComplete="off"
            spellCheck={false}
            maxLength={320}
            onChange={(e) => change(e.target.value)}
          />
        </label>
        <Button type="submit" variant="primary" disabled={!typed || busy}>
          {busy ? 'Finding…' : 'Find'}
        </Button>
      </form>
      {error ? <ProblemNotice error={error} /> : null}
      {found ? (
        <div role="status" aria-label={`${title}: result`}>
          <Table
            caption={caption(found.asked)}
            columns={columns}
            rows={found.rows}
            rowKey={rowKey}
            empty={none(found.asked)}
            emptyHint={noneHint}
          />
        </div>
      ) : null}
    </section>
  );
}

const PERSON_COLUMNS: readonly Column<UserMatch>[] = [
  { header: 'Name', cell: (u) => u.display_name },
  { header: 'Email', cell: (u) => u.email },
  { header: 'Role', cell: (u) => <Badge tone={u.role === 'admin' ? 'info' : 'neutral'}>{u.role}</Badge> },
  {
    header: 'Status',
    cell: (u) => <Badge tone={u.status === 'active' ? 'success' : 'danger'}>{u.status}</Badge>,
  },
  { header: 'Id', cell: (u) => <code>{u.id}</code> },
];

function FindPerson() {
  const { api } = Route.useRouteContext();
  return (
    <Finder
      title="Look up a person"
      label="Email address"
      placeholder="name@example.com"
      inputType="email"
      find={(email) => peopleByEmail(api, email)}
      caption={(email) => `People with the email ${email}`}
      columns={PERSON_COLUMNS}
      rowKey={(u) => u.id}
      none={(email) => `No one in the organisation has the address ${email}.`}
      noneHint="The whole address must match. People are added in your identity provider."
    >
      By the whole email address, ignoring case. A deactivated person is shown too. To change a
      person&apos;s role, name or status, change them in your identity provider.
    </Finder>
  );
}

const GROUP_COLUMNS: readonly Column<GroupMatch>[] = [
  { header: 'Name', cell: (g) => g.name },
  {
    header: 'Members',
    cell: (g) => `${g.member_count} active ${g.member_count === 1 ? 'member' : 'members'}`,
  },
  { header: 'Id', cell: (g) => <code>{g.id}</code> },
];

function FindGroup() {
  const { api } = Route.useRouteContext();
  return (
    <Finder
      title="Look up a group"
      label="Group name"
      placeholder="Finance"
      find={(name) => groupsByName(api, name)}
      caption={(name) => `Groups named ${name}`}
      columns={GROUP_COLUMNS}
      rowKey={(g) => g.id}
      none={(name) => `No group is named exactly ${name}.`}
      noneHint="The whole name must match. Groups and their members come from your identity provider."
    >
      By the whole name, ignoring case. Only active members are counted. To change a group or
      who is in it, change it in your identity provider.
    </Finder>
  );
}
