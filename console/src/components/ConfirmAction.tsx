import { type FormEvent, type ReactNode, useId, useState } from 'react';
import { Button } from './Button';
import { Dialog } from './Dialog';
import { ProblemNotice } from './ProblemNotice';

interface Props {
  /** The button that opens the dialog, for example "Remove access". */
  readonly label: string;
  /** Screen-reader name of the opening button when `label` alone is ambiguous in a list. */
  readonly accessibleLabel?: string;
  readonly title: string;
  readonly children: ReactNode;
  /** The text the user must type, normally the app slug. */
  readonly confirmText: string;
  readonly confirmLabel: string;
  readonly onConfirm: () => Promise<unknown>;
  readonly disabled?: boolean;
}

/** A destructive action that runs only after the user types `confirmText` exactly. */
export function ConfirmAction(props: Props) {
  const { label, accessibleLabel, title, children, confirmText, confirmLabel, onConfirm, disabled } =
    props;
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const inputId = useId();

  function close() {
    setOpen(false);
    setTyped('');
    setError(null);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (typed !== confirmText || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
      close();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button
        variant="danger"
        disabled={disabled}
        aria-label={accessibleLabel}
        onClick={() => setOpen(true)}
      >
        {label}
      </Button>
      <Dialog open={open} title={title} onClose={close}>
        <form className="stack" onSubmit={submit}>
          <div>{children}</div>
          <label className="field" htmlFor={inputId}>
            <span>
              Type <code>{confirmText}</code> to confirm
            </span>
            <input
              id={inputId}
              value={typed}
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => setTyped(e.target.value)}
            />
          </label>
          {error ? <ProblemNotice error={error} /> : null}
          <div className="actions">
            <Button onClick={close}>Cancel</Button>
            <Button type="submit" variant="danger" disabled={typed !== confirmText || busy}>
              {confirmLabel}
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
