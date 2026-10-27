import { useRouteContext } from '@tanstack/react-router';
import { type FormEvent, useId, useState } from 'react';
import {
  ADDRESS_FIELDS,
  addressComplete,
  AVAILABLE_KINDS,
  type Ceiling,
  type Classification,
  createConnection,
  defaultPreviewWho,
  emptyAddress,
  type Kind,
  KIND_TITLE,
  type Limits,
  needsCeiling,
  parseAddress,
  parseLimits,
  parseSchemas,
  previewAccessOf,
  type PreviewWho,
  SQL_KINDS,
} from '../api/connections';
import { findPeople, type Match, USER_ID_PATTERN } from '../api/directory';
import { Lookup } from '../app-detail/Lookup';
import { Button } from '../components/Button';
import { Dialog } from '../components/Dialog';
import { ProblemNotice } from '../components/ProblemNotice';
import { useToast } from '../components/Toast';
import {
  CeilingFields,
  ceilingOf,
  ClassificationField,
  LimitFields,
  PreviewAccessFields,
  SchemasField,
  type Typed,
} from './fields';
import { useRefreshConnections } from './save';

const FIRST_KIND: Kind = AVAILABLE_KINDS[0] ?? 'postgres';

export function AddConnection() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button variant="primary" onClick={() => setOpen(true)}>
        Add connection
      </Button>
      <Dialog open={open} title="Add a connection" onClose={() => setOpen(false)}>
        <AddForm onClose={() => setOpen(false)} />
      </Dialog>
    </>
  );
}

/**
 * The address lives only in this form's state, goes to the API in the request body and is
 * cleared as soon as the request is sent; it never enters a query key, the cache or the URL.
 */
function AddForm({ onClose }: { readonly onClose: () => void }) {
  const { api } = useRouteContext({ from: '/_authed' });
  const refresh = useRefreshConnections();
  const toast = useToast();
  const [name, setName] = useState('');
  const [kind, setKind] = useState<Kind>(FIRST_KIND);
  const [classification, setClassification] = useState<Classification>('internal');
  const [owner, setOwner] = useState<Match | null>(null);
  const [address, setAddress] = useState<Record<string, string>>(() => emptyAddress(FIRST_KIND));
  const [schemas, setSchemas] = useState('public');
  const [audience, setAudience] = useState<Ceiling['audience']>('org');
  const [subjects, setSubjects] = useState<Match[]>([]);
  const [limits, setLimits] = useState<Typed>({});
  const [previewWho, setPreviewWho] = useState<PreviewWho>(defaultPreviewWho('internal'));
  const [previewTouched, setPreviewTouched] = useState(false);
  const [previewGroup, setPreviewGroup] = useState<Match[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const nameId = useId();
  const kindId = useId();
  const addressId = useId();
  const sql = SQL_KINDS.includes(kind);

  function pickKind(next: Kind) {
    setKind(next);
    setAddress(emptyAddress(next));
  }

  function pickClassification(next: Classification) {
    setClassification(next);
    if (!previewTouched) setPreviewWho(defaultPreviewWho(next));
  }

  function pickPreviewWho(next: PreviewWho) {
    setPreviewWho(next);
    setPreviewTouched(true);
  }

  const ready = name.trim() !== '' && owner !== null && addressComplete(kind, address);
  const addressEmpty = Object.entries(address).every(([k, v]) => (k === 'port' && sql) || v.trim() === '');

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready || !owner || busy) return;
    setError(null);
    let ceiling: Ceiling;
    let parsedLimits: Limits;
    let parsedAddress: Record<string, string | number>;
    let previewAccess: ReturnType<typeof previewAccessOf>;
    try {
      ceiling = ceilingOf(audience, subjects);
      previewAccess = previewAccessOf(previewWho, previewGroup[0]?.id ?? '');
      parsedLimits = parseLimits(limits);
      parsedAddress = parseAddress(kind, address);
    } catch (e) {
      setError(e);
      return;
    }
    const added = name.trim();
    const where = sql
      ? { host: String(parsedAddress.host), port: Number(parsedAddress.port), database: String(parsedAddress.database) }
      : { address: parsedAddress };
    const request = createConnection(api, {
      name: added,
      kind,
      owner_user_id: owner.id,
      classification,
      ceiling,
      allowed_schemas: parseSchemas(schemas),
      limits: parsedLimits,
      preview_access: previewAccess,
      ...where,
    });
    setAddress(emptyAddress(kind));
    setBusy(true);
    try {
      await request;
      await refresh();
      toast(`Added ${added}. It stays pending until your operator has set up its credentials.`);
      onClose();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="stack" onSubmit={submit} aria-label="Add a connection" autoComplete="off">
      <label className="field" htmlFor={nameId}>
        <span>Name, lower case letters, digits and dashes</span>
        <input id={nameId} value={name} spellCheck={false} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="field" htmlFor={kindId}>
        <span>Kind of source</span>
        <select id={kindId} value={kind} onChange={(e) => pickKind(e.target.value as Kind)}>
          {AVAILABLE_KINDS.map((k) => (
            <option key={k} value={k}>
              {KIND_TITLE[k]}
            </option>
          ))}
        </select>
      </label>
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
        <legend>Address, stored and never shown again. Credentials go to your operator, never here.</legend>
        {ADDRESS_FIELDS[kind].map((f) => (
          <label className="field" htmlFor={`${addressId}-${f.key}`} key={f.key}>
            <span>{f.label}</span>
            <input
              id={`${addressId}-${f.key}`}
              inputMode={f.numeric ? 'numeric' : undefined}
              value={address[f.key] ?? ''}
              spellCheck={false}
              autoComplete="off"
              onChange={(e) => setAddress({ ...address, [f.key]: e.target.value })}
            />
          </label>
        ))}
      </fieldset>
      <details>
        <summary>More options</summary>
        <div className="stack">
          <ClassificationField value={classification} onChange={pickClassification} />
          <CeilingFields audience={audience} subjects={subjects} onAudience={setAudience} onSubjects={setSubjects} />
          {needsCeiling(classification) && audience === 'org' ? (
            <p className="muted">
              A {classification} connection may still allow the whole organisation, but say so on purpose.
            </p>
          ) : null}
          {sql ? <SchemasField value={schemas} onChange={setSchemas} /> : null}
          <PreviewAccessFields
            who={previewWho}
            group={previewGroup}
            onWho={pickPreviewWho}
            onGroup={setPreviewGroup}
          />
          <LimitFields typed={limits} onChange={setLimits} />
        </div>
      </details>
      {error ? <ProblemNotice error={error} /> : null}
      {error && addressEmpty ? <p className="muted">The address is cleared once sent; enter it again to retry.</p> : null}
      <div className="actions">
        <Button onClick={onClose}>Cancel</Button>
        <Button type="submit" variant="primary" disabled={!ready || busy}>
          Add connection
        </Button>
      </div>
    </form>
  );
}
