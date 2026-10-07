import { createFileRoute } from '@tanstack/react-router';
import { type FormEvent, useId, useState } from 'react';
import {
  type Ceiling,
  ceilingText,
  changeConnection,
  CLASSIFICATIONS,
  type Classification,
  type Connection,
  type ConnectionChange,
  createConnection,
  LIMIT_KEYS,
  LIMIT_TITLE,
  type Limits,
  limitsText,
  limitsTyped,
  needsCeiling,
  parseLimits,
  parseSchemas,
  parseSubjects,
  subjectsText,
} from '../../api/connections';
import { findPeople, type Match, USER_ID_PATTERN } from '../../api/directory';
import { Lookup } from '../../app-detail/Lookup';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { Dialog } from '../../components/Dialog';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';

export const Route = createFileRoute('/_authed/connections')({
  component: ConnectionsPage,
});

type Typed = Partial<Record<keyof Limits, string>>;

function ConnectionsPage() {
  const { queries, isAdmin } = Route.useRouteContext();
  const me = queries.useQuery('get', '/v1/whoami');
  const list = queries.useQuery('get', '/v1/connections');
  const [notice, setNotice] = useState<string | null>(null);
  const admin = isAdmin(me.data);

  const columns: Column<Connection>[] = [
    { header: 'Name', cell: (c) => <code>{c.name}</code> },
    { header: 'Classification', cell: (c) => c.classification },
    { header: 'Apps may be shared with', cell: (c) => ceilingText(c.ceiling) },
    { header: 'Schemas', cell: (c) => c.allowed_schemas.join(', ') },
    { header: 'Limits', cell: (c) => limitsText(c.limits) },
    {
      header: 'State',
      cell: (c) => (
        <>
          <Badge tone={c.status === 'active' ? 'success' : 'danger'}>{c.status}</Badge>{' '}
          <Badge tone={c.setup_status === 'ready' ? 'success' : 'warning'}>{c.setup_status}</Badge>
        </>
      ),
    },
    {
      header: 'Owner',
      cell: (c) => (c.owner_user_id ? <code>{c.owner_user_id}</code> : 'None'),
    },
  ];
  if (admin) {
    columns.push({
      header: 'Action',
      className: 'actions-cell',
      cell: (c) => <EditConnection connection={c} onDone={setNotice} />,
    });
  }

  return (
    <>
      <div className="page-head">
        <h1>Connections</h1>
      </div>
      <p className="muted">
        Your company&apos;s databases that apps may read through the data gateway. A connection&apos;s
        address is stored when it is added and never shown again; its credentials are set up with
        your operator.
      </p>
      {me.isPending ? null : admin ? (
        <div className="toolbar">
          <AddConnection onDone={setNotice} />
        </div>
      ) : (
        <p className="muted">
          Only org admins can add or change connections. You see the ones your approved requests
          name.
        </p>
      )}
      {notice ? (
        <p className="notice notice-success" role="status">
          {notice}
        </p>
      ) : null}
      <section className="panel" aria-label="Connections">
        {list.isPending ? (
          <p className="muted">Loading connections…</p>
        ) : list.isError ? (
          <ProblemNotice error={list.error} />
        ) : (
          <Table
            caption="Connections"
            columns={columns}
            rows={list.data.connections}
            rowKey={(c) => c.id}
            empty="No connections yet."
          />
        )}
      </section>
    </>
  );
}

function useRefreshList() {
  const { queries, queryClient } = Route.useRouteContext();
  return () =>
    queryClient.invalidateQueries({ queryKey: queries.queryOptions('get', '/v1/connections').queryKey });
}

interface CeilingProps {
  readonly audience: Ceiling['audience'];
  readonly subjects: string;
  readonly onAudience: (audience: Ceiling['audience']) => void;
  readonly onSubjects: (text: string) => void;
}

function CeilingFields({ audience, subjects, onAudience, onSubjects }: CeilingProps) {
  const name = useId();
  const subjectsId = useId();
  return (
    <>
      <fieldset className="choices">
        <legend>Apps on it may be shared with</legend>
        <label className="check">
          <input
            type="radio"
            name={name}
            checked={audience === 'org'}
            onChange={() => onAudience('org')}
          />
          <span>Anyone in the organisation</span>
        </label>
        <label className="check">
          <input
            type="radio"
            name={name}
            checked={audience === 'subjects'}
            onChange={() => onAudience('subjects')}
          />
          <span>Only the groups and people listed</span>
        </label>
      </fieldset>
      {audience === 'subjects' ? (
        <label className="field" htmlFor={subjectsId}>
          <span>Groups and people, one grp_ or usr_ id per line</span>
          <textarea
            id={subjectsId}
            rows={3}
            value={subjects}
            spellCheck={false}
            onChange={(e) => onSubjects(e.target.value)}
          />
        </label>
      ) : null}
    </>
  );
}

function LimitFields({ typed, onChange }: { readonly typed: Typed; readonly onChange: (t: Typed) => void }) {
  const prefix = useId();
  return (
    <fieldset className="choices">
      <legend>Limits, empty for none</legend>
      {LIMIT_KEYS.map((key) => (
        <label key={key} className="field" htmlFor={`${prefix}-${key}`}>
          <span>{LIMIT_TITLE[key]}</span>
          <input
            id={`${prefix}-${key}`}
            inputMode="numeric"
            value={typed[key] ?? ''}
            onChange={(e) => onChange({ ...typed, [key]: e.target.value })}
          />
        </label>
      ))}
    </fieldset>
  );
}

function ClassificationField({
  value,
  onChange,
}: {
  readonly value: Classification;
  readonly onChange: (c: Classification) => void;
}) {
  const id = useId();
  return (
    <label className="field" htmlFor={id}>
      <span>Classification</span>
      <select id={id} value={value} onChange={(e) => onChange(e.target.value as Classification)}>
        {CLASSIFICATIONS.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
    </label>
  );
}

function ceilingOf(audience: Ceiling['audience'], subjects: string): Ceiling {
  return audience === 'org' ? { audience: 'org' } : { audience: 'subjects', subjects: parseSubjects(subjects) };
}

/**
 * Adds a connection. The address (host, port, database) lives only in this form's state, goes
 * to the API in the request body and is cleared as soon as the request is sent, whatever the
 * answer; it never enters a query key, the cache or the URL.
 */
function AddConnection({ onDone }: { readonly onDone: (message: string) => void }) {
  const { api } = Route.useRouteContext();
  const refresh = useRefreshList();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [classification, setClassification] = useState<Classification>('internal');
  const [owner, setOwner] = useState<Match | null>(null);
  const [host, setHost] = useState('');
  const [port, setPort] = useState('5432');
  const [database, setDatabase] = useState('');
  const [schemas, setSchemas] = useState('public');
  const [audience, setAudience] = useState<Ceiling['audience']>('org');
  const [subjects, setSubjects] = useState('');
  const [limits, setLimits] = useState<Typed>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const nameId = useId();
  const hostId = useId();
  const portId = useId();
  const databaseId = useId();
  const schemasId = useId();

  function forgetAddress() {
    setHost('');
    setPort('5432');
    setDatabase('');
  }

  function close() {
    setOpen(false);
    setName('');
    setClassification('internal');
    setOwner(null);
    forgetAddress();
    setSchemas('public');
    setAudience('org');
    setSubjects('');
    setLimits({});
    setError(null);
  }

  const ready = name.trim() !== '' && owner !== null && host.trim() !== '' && database.trim() !== '';

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready || !owner || busy) return;
    setError(null);
    let ceiling: Ceiling;
    let parsedLimits: Limits;
    try {
      ceiling = ceilingOf(audience, subjects);
      parsedLimits = parseLimits(limits);
    } catch (e) {
      setError(e);
      return;
    }
    if (!/^\d+$/.test(port.trim())) {
      setError(new Error('Port must be a whole number.'));
      return;
    }
    const added = name.trim();
    const request = createConnection(api, {
      name: added,
      kind: 'postgres',
      owner_user_id: owner.id,
      classification,
      ceiling,
      allowed_schemas: parseSchemas(schemas),
      limits: parsedLimits,
      host: host.trim(),
      port: Number(port.trim()),
      database: database.trim(),
    });
    forgetAddress();
    setBusy(true);
    try {
      await request;
      await refresh();
      onDone(`Added ${added}. It stays pending until your operator has set up its credentials.`);
      close();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button variant="primary" onClick={() => setOpen(true)}>
        Add a connection
      </Button>
      <Dialog open={open} title="Add a connection" onClose={close}>
        <form className="stack" onSubmit={submit} aria-label="Add a connection" autoComplete="off">
          <label className="field" htmlFor={nameId}>
            <span>Name, lower case letters, digits and dashes</span>
            <input
              id={nameId}
              value={name}
              spellCheck={false}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <ClassificationField value={classification} onChange={setClassification} />
          <Lookup
            label="Owner: email or usr_ id"
            placeholder="name@example.com"
            idPattern={USER_ID_PATTERN}
            search={(email) => findPeople(api, email)}
            hint="The owner decides when an app on it is shared wider than the connection allows."
            picked={owner}
            onPick={setOwner}
          />
          <fieldset className="choices">
            <legend>Address, stored and never shown again</legend>
            <label className="field" htmlFor={hostId}>
              <span>Host</span>
              <input
                id={hostId}
                value={host}
                spellCheck={false}
                autoComplete="off"
                onChange={(e) => setHost(e.target.value)}
              />
            </label>
            <label className="field" htmlFor={portId}>
              <span>Port</span>
              <input
                id={portId}
                inputMode="numeric"
                value={port}
                autoComplete="off"
                onChange={(e) => setPort(e.target.value)}
              />
            </label>
            <label className="field" htmlFor={databaseId}>
              <span>Database</span>
              <input
                id={databaseId}
                value={database}
                spellCheck={false}
                autoComplete="off"
                onChange={(e) => setDatabase(e.target.value)}
              />
            </label>
          </fieldset>
          <label className="field" htmlFor={schemasId}>
            <span>Schemas apps may read, separated by commas</span>
            <input
              id={schemasId}
              value={schemas}
              spellCheck={false}
              onChange={(e) => setSchemas(e.target.value)}
            />
          </label>
          <CeilingFields
            audience={audience}
            subjects={subjects}
            onAudience={setAudience}
            onSubjects={setSubjects}
          />
          {needsCeiling(classification) && audience === 'org' ? (
            <p className="muted">
              A {classification} connection may still allow the whole organisation, but say so on
              purpose.
            </p>
          ) : null}
          <LimitFields typed={limits} onChange={setLimits} />
          {error ? <ProblemNotice error={error} /> : null}
          {error && !host && !database ? (
            <p className="muted">The address is cleared once sent; enter it again to retry.</p>
          ) : null}
          <div className="actions">
            <Button onClick={close}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={!ready || busy}>
              Add connection
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function EditConnection({
  connection,
  onDone,
}: {
  readonly connection: Connection;
  readonly onDone: (message: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const title = `Change ${connection.name}`;
  return (
    <>
      <Button onClick={() => setOpen(true)} aria-label={title}>
        Change
      </Button>
      <Dialog open={open} title={title} onClose={() => setOpen(false)}>
        {open ? (
          <EditForm
            connection={connection}
            title={title}
            onClose={() => setOpen(false)}
            onDone={(message) => {
              onDone(message);
              setOpen(false);
            }}
          />
        ) : null}
      </Dialog>
    </>
  );
}

interface EditProps {
  readonly connection: Connection;
  readonly title: string;
  readonly onClose: () => void;
  readonly onDone: (message: string) => void;
}

/** Sends only what changed. A move to confidential or restricted carries the ceiling with it. */
function EditForm({ connection, title, onClose, onDone }: EditProps) {
  const { api } = Route.useRouteContext();
  const refresh = useRefreshList();
  const [classification, setClassification] = useState(connection.classification);
  const [owner, setOwner] = useState(connection.owner_user_id ?? '');
  const [schemas, setSchemas] = useState(connection.allowed_schemas.join(', '));
  const [audience, setAudience] = useState(connection.ceiling.audience);
  const [subjects, setSubjects] = useState(subjectsText(connection.ceiling));
  const [limits, setLimits] = useState<Typed>(limitsTyped(connection.limits));
  const [setup, setSetup] = useState(connection.setup_status);
  const [status, setStatus] = useState(connection.status);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const ownerId = useId();
  const schemasId = useId();
  const setupId = useId();
  const statusId = useId();

  function change(): ConnectionChange {
    const patch: ConnectionChange = {};
    const ceiling = ceilingOf(audience, subjects);
    if (classification !== connection.classification) patch.classification = classification;
    const before = ceilingOf(connection.ceiling.audience, subjectsText(connection.ceiling));
    if (!same(ceiling, before)) patch.ceiling = ceiling;
    if (patch.classification && needsCeiling(patch.classification)) patch.ceiling = ceiling;
    const typedOwner = owner.trim();
    if (typedOwner !== (connection.owner_user_id ?? '')) {
      if (!USER_ID_PATTERN.test(typedOwner)) throw new Error('The owner must be a usr_ id.');
      patch.owner_user_id = typedOwner;
    }
    const parsedSchemas = parseSchemas(schemas);
    if (!same(parsedSchemas, connection.allowed_schemas)) patch.allowed_schemas = parsedSchemas;
    const parsedLimits = parseLimits(limits);
    if (!same(parsedLimits, parseLimits(limitsTyped(connection.limits)))) patch.limits = parsedLimits;
    if (setup !== connection.setup_status) patch.setup_status = setup;
    if (status !== connection.status) patch.status = status;
    return patch;
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError(null);
    let patch: ConnectionChange;
    try {
      patch = change();
    } catch (e) {
      setError(e);
      return;
    }
    if (Object.keys(patch).length === 0) {
      onClose();
      return;
    }
    setBusy(true);
    try {
      await changeConnection(api, connection.name, patch);
      await refresh();
      onDone(`Changed ${connection.name}.`);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="stack" onSubmit={submit} aria-label={title}>
      <ClassificationField value={classification} onChange={setClassification} />
      <label className="field" htmlFor={ownerId}>
        <span>Owner (usr_ id)</span>
        <input
          id={ownerId}
          value={owner}
          spellCheck={false}
          autoComplete="off"
          onChange={(e) => setOwner(e.target.value)}
        />
      </label>
      <label className="field" htmlFor={schemasId}>
        <span>Schemas apps may read, separated by commas</span>
        <input
          id={schemasId}
          value={schemas}
          spellCheck={false}
          onChange={(e) => setSchemas(e.target.value)}
        />
      </label>
      <CeilingFields
        audience={audience}
        subjects={subjects}
        onAudience={setAudience}
        onSubjects={setSubjects}
      />
      <p className="muted">
        A narrower audience flags every environment now shared wider; nothing stops until someone
        acts on the flag.
      </p>
      <LimitFields typed={limits} onChange={setLimits} />
      <label className="field" htmlFor={setupId}>
        <span>Setup</span>
        <select id={setupId} value={setup} onChange={(e) => setSetup(e.target.value as Connection['setup_status'])}>
          <option value="pending">pending: credentials not set up yet</option>
          <option value="ready">ready: the runbook&apos;s first read worked</option>
        </select>
      </label>
      <label className="field" htmlFor={statusId}>
        <span>Status</span>
        <select id={statusId} value={status} onChange={(e) => setStatus(e.target.value as Connection['status'])}>
          <option value="active">active</option>
          <option value="suspended">suspended: every query on it stops</option>
        </select>
      </label>
      {error ? <ProblemNotice error={error} /> : null}
      <div className="actions">
        <Button onClick={onClose}>Cancel</Button>
        <Button type="submit" variant="primary" disabled={busy}>
          Save changes
        </Button>
      </div>
    </form>
  );
}
