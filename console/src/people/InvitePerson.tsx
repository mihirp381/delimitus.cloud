import { useRouteContext } from '@tanstack/react-router';
import { type FormEvent, useId, useState } from 'react';
import { invitePerson } from '../api/people';
import { Button } from '../components/Button';
import { Dialog } from '../components/Dialog';
import { ProblemNotice } from '../components/ProblemNotice';
import { useToast } from '../components/Toast';

const TITLE = 'Invite by email';

export function InvitePerson() {
  const { api, queryClient } = useRouteContext({ from: '/_authed' });
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const emailId = useId();

  function close() {
    setOpen(false);
    setEmail('');
    setError(null);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const address = email.trim();
    if (!address || busy) return;
    setBusy(true);
    setError(null);
    try {
      const person = await invitePerson(api, address);
      await queryClient.invalidateQueries({ queryKey: ['users'] });
      toast(
        person.status === 'invited'
          ? `Invited ${person.email}. They join the first time they sign in.`
          : `${person.email} is already here (${person.status}).`,
      );
      close();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button variant="primary" onClick={() => setOpen(true)}>
        {TITLE}
      </Button>
      <Dialog open={open} title={TITLE} onClose={close}>
        <form className="stack" onSubmit={submit} aria-label={TITLE}>
          <p>
            They sign in under this address with one of your organisation&apos;s sign-in
            providers, and become a member the first time they do.
          </p>
          <label className="field" htmlFor={emailId}>
            <span>Email</span>
            <input
              id={emailId}
              type="email"
              value={email}
              maxLength={320}
              autoComplete="off"
              placeholder="name@example.com"
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          {error ? <ProblemNotice error={error} /> : null}
          <div className="actions">
            <Button onClick={close}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={!email.trim() || busy}>
              {busy ? 'Inviting…' : 'Invite'}
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
