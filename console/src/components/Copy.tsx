import { useEffect, useState } from 'react';
import { Icon } from './Icon';

interface CopyProps {
  readonly value: string;
  /** What is copied, for the button's name: "Copy app id". */
  readonly label: string;
}

/** How long the tick stays after a copy. */
const SHOWN_MS = 1600;

/**
 * A small button that copies `value`. It says what happened to a screen reader and shows a tick;
 * where the browser refuses the clipboard it says so, and the text beside it can still be selected.
 */
export function CopyButton({ value, label }: CopyProps) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  useEffect(() => {
    if (state === 'idle') return undefined;
    const timer = window.setTimeout(() => setState('idle'), SHOWN_MS);
    return () => window.clearTimeout(timer);
  }, [state]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setState('copied');
    } catch {
      setState('failed');
    }
  }

  return (
    <button type="button" className="copy" aria-label={`Copy ${label}`} title={`Copy ${label}`} onClick={() => void copy()}>
      <Icon name={state === 'copied' ? 'check' : 'copy'} />
      <span className="visually-hidden" role="status">
        {state === 'copied' ? 'Copied.' : state === 'failed' ? 'Could not copy; select the text instead.' : ''}
      </span>
    </button>
  );
}

interface IdProps {
  readonly value: string;
  /** What the id names, for the copy button: "app id". */
  readonly label: string;
  /** Let a long id give way to its neighbours; its title carries all of it. */
  readonly truncate?: boolean;
}

/** An id in the mono face with the button that copies it. */
export function Id({ value, label, truncate }: IdProps) {
  return (
    <span className="id">
      <code className={truncate ? 'truncate' : undefined} title={truncate ? value : undefined}>
        {value}
      </code>
      <CopyButton value={value} label={label} />
    </span>
  );
}
