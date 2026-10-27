import { useState } from 'react';
import {
  ceilingText,
  type Connection,
  type ConnectionChange,
  limitPhrases,
  limitsTyped,
  parseLimits,
  parseSchemas,
  SCHEMAS_NOTE,
  subjectMatches,
} from '../api/connections';
import type { Match } from '../api/directory';
import { Phrases } from '../components/Phrases';
import { ChangeForm, EditablePanel } from './Editable';
import { CeilingFields, ceilingOf, LimitFields, SchemasField, type Typed } from './fields';
import { same } from './save';

type Props = { readonly connection: Connection };
type FormProps = Props & { readonly onClose: () => void };

export function Schemas({ connection: c }: Props) {
  return (
    <EditablePanel
      title="Schemas"
      view={
        <>
          {c.allowed_schemas.length > 0 ? <Phrases items={c.allowed_schemas} /> : <p className="muted">None listed.</p>}
          <p className="muted">{SCHEMAS_NOTE}</p>
        </>
      }
      form={(close) => <SchemasForm connection={c} onClose={close} />}
    />
  );
}

function SchemasForm({ connection: c, onClose }: FormProps) {
  const [schemas, setSchemas] = useState(c.allowed_schemas.join(', '));
  function build(): ConnectionChange {
    const parsed = parseSchemas(schemas);
    return same(parsed, c.allowed_schemas) ? {} : { allowed_schemas: parsed };
  }
  return (
    <ChangeForm label={`Change the schemas of ${c.name}`} connection={c} build={build} onClose={onClose}>
      <SchemasField value={schemas} onChange={setSchemas} />
    </ChangeForm>
  );
}

export function Limits({ connection: c }: Props) {
  const phrases = limitPhrases(c.limits);
  return (
    <EditablePanel
      title="Limits and ceiling"
      view={
        <dl className="facts">
          <dt>Limits</dt>
          <dd>{phrases.length > 0 ? <Phrases items={phrases} /> : 'None'}</dd>
          <dt>Ceiling</dt>
          <dd>{ceilingText(c)}</dd>
        </dl>
      }
      form={(close) => <LimitsForm connection={c} onClose={close} />}
    />
  );
}

function LimitsForm({ connection: c, onClose }: FormProps) {
  const [audience, setAudience] = useState(c.ceiling.audience);
  const [subjects, setSubjects] = useState<Match[]>(() => subjectMatches(c));
  const [limits, setLimits] = useState<Typed>(limitsTyped(c.limits));

  function build(): ConnectionChange {
    const patch: ConnectionChange = {};
    const ceiling = ceilingOf(audience, subjects);
    if (!same(ceiling, ceilingOf(c.ceiling.audience, subjectMatches(c)))) patch.ceiling = ceiling;
    const parsed = parseLimits(limits);
    if (!same(parsed, parseLimits(limitsTyped(c.limits)))) patch.limits = parsed;
    return patch;
  }

  return (
    <ChangeForm label={`Change the limits and ceiling of ${c.name}`} connection={c} build={build} onClose={onClose}>
      <CeilingFields audience={audience} subjects={subjects} onAudience={setAudience} onSubjects={setSubjects} />
      <p className="muted">
        A narrower audience flags every environment now shared wider; nothing stops until someone acts on the flag.
      </p>
      <LimitFields typed={limits} onChange={setLimits} />
    </ChangeForm>
  );
}
