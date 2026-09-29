import { type FormEvent, useState } from 'react';
import { type ApiClient, must } from '../api/client';
import type { Session } from './session';
import { Button } from '../components/Button';
import { ProblemNotice } from '../components/ProblemNotice';

/** The build-output test looks for this string; it must appear only in this file. */
export const DEV_LOGIN_MARKER = 'ssc-dev-login';

interface Props {
  readonly api: ApiClient;
  readonly session: Session;
  readonly onSignedIn: () => void;
}

/** Paste a token from `tools/dev_stack.py token`. Never part of a production build. */
export default function DevLogin({ api, session, onSignedIn }: Props) {
  const [token, setToken] = useState('');
  const [keep, setKeep] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const value = token.trim();
    if (!value) return;
    setBusy(true);
    setError(null);
    try {
      must(await api.GET('/v1/whoami', { headers: { Authorization: `Bearer ${value}` } }));
      session.set(value, keep);
      onSignedIn();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="stack" data-testid={DEV_LOGIN_MARKER} onSubmit={submit}>
      <p className="muted">
        Development sign-in. Paste a token from <code>tools/dev_stack.py token</code>.
      </p>
      <label className="field">
        <span>API token</span>
        <textarea
          name="token"
          rows={4}
          value={token}
          spellCheck={false}
          autoComplete="off"
          onChange={(e) => setToken(e.target.value)}
        />
      </label>
      <label className="check">
        <input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} />
        <span>Keep it for this tab</span>
      </label>
      {error ? <ProblemNotice error={error} /> : null}
      <div>
        <Button type="submit" variant="primary" disabled={busy || !token.trim()}>
          Sign in
        </Button>
      </div>
    </form>
  );
}
