import { type KeyboardEvent, type ReactNode, useId, useState } from 'react';
import type { Match } from '../api/directory';
import { Button } from '../components/Button';
import { ProblemNotice } from '../components/ProblemNotice';

interface Props {
  readonly label: string;
  readonly placeholder: string;
  /** Text matching this is taken as the id itself, with no search. */
  readonly idPattern: RegExp;
  /** Finds matches for other text; without it only an id is accepted. */
  readonly search?: (text: string) => Promise<readonly Match[]>;
  readonly hint?: ReactNode;
  readonly picked: Match | null;
  readonly onPick: (match: Match | null) => void;
}

/** A text box that takes an id as typed, or searches and lets the user pick one match. */
export function Lookup({ label, placeholder, idPattern, search, hint, picked, onPick }: Props) {
  const [text, setText] = useState('');
  const [matches, setMatches] = useState<readonly Match[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const inputId = useId();
  const name = useId();
  const typed = text.trim();
  const isId = idPattern.test(typed);

  function change(value: string) {
    setText(value);
    setMatches(null);
    setError(null);
    const v = value.trim();
    onPick(idPattern.test(v) ? { id: v, label: v } : null);
  }

  async function find() {
    if (!search || !typed || isId || busy) return;
    setBusy(true);
    setError(null);
    try {
      const found = await search(typed);
      setMatches(found);
      const open = found.filter((m) => !m.unavailable);
      onPick(open.length === 1 && open[0] ? open[0] : null);
    } catch (e) {
      setMatches(null);
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter' && search && !isId) {
      event.preventDefault();
      void find();
    }
  }

  return (
    <div className="stack">
      <div className="field">
        <label htmlFor={inputId}>{label}</label>
        <div className="lookup">
          <input
            id={inputId}
            value={text}
            placeholder={placeholder}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => change(e.target.value)}
            onKeyDown={onKeyDown}
          />
          {search ? (
            <Button onClick={() => void find()} disabled={!typed || isId || busy}>
              Find
            </Button>
          ) : null}
        </div>
      </div>
      {hint ? <p className="muted">{hint}</p> : null}
      {error ? <ProblemNotice error={error} /> : null}
      {matches && matches.length === 0 ? (
        <p className="empty">Nothing matches {typed}.</p>
      ) : null}
      {matches && matches.length > 0 ? (
        <fieldset className="choices">
          <legend className="visually-hidden">Matches for {typed}</legend>
          {matches.map((m) => (
            <label key={m.id} className="check">
              <input
                type="radio"
                name={name}
                value={m.id}
                checked={picked?.id === m.id}
                disabled={Boolean(m.unavailable)}
                onChange={() => onPick(m)}
              />
              <span>
                {m.label} <code className="muted">{m.id}</code>
                {m.detail ? <span className="muted"> · {m.detail}</span> : null}
                {m.unavailable ? <span className="muted"> · {m.unavailable}</span> : null}
              </span>
            </label>
          ))}
        </fieldset>
      ) : null}
    </div>
  );
}
