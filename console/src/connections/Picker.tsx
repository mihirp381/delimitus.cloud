import { useState } from 'react';
import type { Match } from '../api/directory';
import { Lookup } from '../app-detail/Lookup';
import { Button } from '../components/Button';
import { Icon } from '../components/Icon';

interface Props {
  readonly legend: string;
  readonly label: string;
  readonly placeholder: string;
  readonly idPattern: RegExp;
  readonly search: (text: string) => Promise<readonly Match[]>;
  readonly picked: readonly Match[];
  readonly onChange: (picked: Match[]) => void;
  readonly single?: boolean;
}

export function Picker({ legend, label, placeholder, idPattern, search, picked, onChange, single }: Props) {
  const [match, setMatch] = useState<Match | null>(null);
  const [round, setRound] = useState(0);

  function add() {
    if (!match) return;
    onChange(single ? [match] : [...picked.filter((p) => p.id !== match.id), match]);
    setMatch(null);
    setRound((r) => r + 1);
  }

  return (
    <fieldset className="choices">
      <legend>{legend}</legend>
      {picked.length > 0 ? (
        <ul className="picks" aria-label={legend}>
          {picked.map((p) => (
            <li key={p.id} className="pick">
              <span title={p.detail}>{p.label}</span>
              <button
                type="button"
                className="icon-btn"
                aria-label={`Remove ${p.label}`}
                onClick={() => onChange(picked.filter((q) => q.id !== p.id))}
              >
                <Icon name="close" />
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">None yet.</p>
      )}
      <Lookup
        key={round}
        label={label}
        placeholder={placeholder}
        idPattern={idPattern}
        search={search}
        picked={match}
        onPick={setMatch}
      />
      <div>
        <Button onClick={add} disabled={!match}>
          {single ? 'Use' : 'Add'}
        </Button>
      </div>
    </fieldset>
  );
}
