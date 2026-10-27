import { type FormEvent, type ReactNode, useState } from 'react';
import type { Connection, ConnectionChange } from '../api/connections';
import { AdminOnly } from '../components/AdminOnly';
import { Button } from '../components/Button';
import { Panel } from '../components/Panel';
import { ProblemNotice } from '../components/ProblemNotice';
import { useConnectionChange } from './save';

interface PanelProps {
  readonly title: string;
  readonly view: ReactNode;
  readonly form: (close: () => void) => ReactNode;
}

export function EditablePanel({ title, view, form }: PanelProps) {
  const [editing, setEditing] = useState(false);
  const close = () => setEditing(false);
  const change = (
    <AdminOnly quiet>
      <Button className="btn-sm" onClick={() => setEditing(true)}>
        Change
      </Button>
    </AdminOnly>
  );
  return (
    <Panel title={title} actions={editing ? undefined : change}>
      {editing ? form(close) : view}
    </Panel>
  );
}

interface FormProps {
  readonly label: string;
  readonly connection: Connection;
  readonly build: () => ConnectionChange;
  readonly onClose: () => void;
  readonly children: ReactNode;
}

export function ChangeForm({ label, connection, build, onClose, children }: FormProps) {
  const save = useConnectionChange(connection.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError(null);
    let patch: ConnectionChange;
    try {
      patch = build();
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
      await save(patch, `Changed ${connection.name}.`);
      onClose();
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  }

  return (
    <form className="stack" onSubmit={submit} aria-label={label} autoComplete="off">
      {children}
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
