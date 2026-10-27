import { useRouteContext } from '@tanstack/react-router';
import { useId } from 'react';
import {
  ADVISORY_LIMITS,
  ADVISORY_NOTE,
  type Ceiling,
  CLASSIFICATIONS,
  type Classification,
  findSubjects,
  LIMIT_KEYS,
  LIMIT_TITLE,
  type Limits,
  PREVIEW_WHO_TITLE,
  type PreviewWho,
  SCHEMAS_NOTE,
  SUBJECT_PATTERN,
  subjectsOf,
} from '../api/connections';
import { findGroups, GROUP_ID_PATTERN, type Match } from '../api/directory';
import { Picker } from './Picker';

export type Typed = Partial<Record<keyof Limits, string>>;

export function ceilingOf(audience: Ceiling['audience'], subjects: readonly Match[]): Ceiling {
  if (audience === 'org') return { audience: 'org' };
  if (subjects.length === 0) throw new Error('Add at least one group or person apps may be shared with.');
  return { audience: 'subjects', subjects: subjectsOf(subjects) };
}

interface CeilingProps {
  readonly audience: Ceiling['audience'];
  readonly subjects: readonly Match[];
  readonly onAudience: (audience: Ceiling['audience']) => void;
  readonly onSubjects: (picked: Match[]) => void;
}

export function CeilingFields({ audience, subjects, onAudience, onSubjects }: CeilingProps) {
  const { api } = useRouteContext({ from: '/_authed' });
  const name = useId();
  return (
    <>
      <fieldset className="choices">
        <legend>Apps on it may be shared with</legend>
        <label className="check">
          <input type="radio" name={name} checked={audience === 'org'} onChange={() => onAudience('org')} />
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
        <Picker
          legend="Groups and people"
          label="Group name, email, or grp_ or usr_ id"
          placeholder="Finance or name@example.com"
          idPattern={SUBJECT_PATTERN}
          search={(text) => findSubjects(api, text)}
          picked={subjects}
          onChange={onSubjects}
        />
      ) : null}
    </>
  );
}

export function LimitFields({ typed, onChange }: { readonly typed: Typed; readonly onChange: (t: Typed) => void }) {
  const prefix = useId();
  return (
    <fieldset className="choices">
      <legend>Limits, empty for none</legend>
      <p className="muted">{ADVISORY_NOTE}</p>
      {LIMIT_KEYS.map((key) => (
        <label key={key} className="field" htmlFor={`${prefix}-${key}`}>
          <span>
            {LIMIT_TITLE[key]}
            {ADVISORY_LIMITS.includes(key) ? ' (advisory)' : ''}
          </span>
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

export function ClassificationField({
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

interface PreviewProps {
  readonly who: PreviewWho;
  readonly group: readonly Match[];
  readonly onWho: (who: PreviewWho) => void;
  readonly onGroup: (group: Match[]) => void;
}

export function PreviewAccessFields({ who, group, onWho, onGroup }: PreviewProps) {
  const { api } = useRouteContext({ from: '/_authed' });
  const id = useId();
  return (
    <>
      <label className="field" htmlFor={id}>
        <span>Preview access</span>
        <select id={id} value={who} onChange={(e) => onWho(e.target.value as PreviewWho)}>
          {(Object.keys(PREVIEW_WHO_TITLE) as PreviewWho[]).map((w) => (
            <option key={w} value={w}>
              {PREVIEW_WHO_TITLE[w]}
            </option>
          ))}
        </select>
      </label>
      {who === 'group' ? (
        <Picker
          single
          legend="Preview access group"
          label="Group name or grp_ id"
          placeholder="Finance"
          idPattern={GROUP_ID_PATTERN}
          search={(text) => findGroups(api, text)}
          picked={group}
          onChange={onGroup}
        />
      ) : null}
    </>
  );
}

export function SchemasField({ value, onChange }: { readonly value: string; readonly onChange: (v: string) => void }) {
  const id = useId();
  return (
    <label className="field" htmlFor={id}>
      <span>Schemas, separated by commas</span>
      <input
        id={id}
        value={value}
        spellCheck={false}
        aria-describedby={`${id}-note`}
        onChange={(e) => onChange(e.target.value)}
      />
      <span id={`${id}-note`} className="muted">
        {SCHEMAS_NOTE}
      </span>
    </label>
  );
}
