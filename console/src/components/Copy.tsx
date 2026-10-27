import { Icon } from './Icon';
import { copyMessage, useClipboard } from './useClipboard';

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
  const { state, copy } = useClipboard(SHOWN_MS);
  return (
    <button type="button" className="copy" aria-label={`Copy ${label}`} title={`Copy ${label}`} onClick={() => void copy(value)}>
      <Icon name={state === 'copied' ? 'check' : 'copy'} />
      <span className="visually-hidden" role="status">
        {copyMessage(state, 'text')}
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
