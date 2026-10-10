import { useRouteContext } from '@tanstack/react-router';
import { type FormEvent, useEffect, useRef, useState } from 'react';
import { type AccessExplained, accessSentence, type ExplainedGrant, explainAccess, grantSubject } from '../api/access';
import { findAnyone, type Match, USER_ID_PATTERN } from '../api/directory';
import type { AppOut, EnvironmentOut } from '../api/lifecycle';
import { Badge } from '../components/Badge';
import { Button } from '../components/Button';
import { ProblemNotice } from '../components/ProblemNotice';
import { type Column, Table } from '../components/Table';
import { Lookup } from './Lookup';
import { ENV_TITLE } from './names';

interface Props {
  readonly app: AppOut;
  readonly env: EnvironmentOut;
}

const GRANT_COLUMNS: readonly Column<ExplainedGrant>[] = [
  { header: 'Role', cell: (g) => g.role },
  { header: 'Who', cell: (g) => grantSubject(g) },
  { header: 'Grant', cell: (g) => <code>{g.grant_id}</code> },
];

/**
 * Why one person can or cannot open an environment: the API runs the gateway's own check on the
 * current sharing rules and names the grants that decided it. Nothing is read until a person is
 * picked and asked about. Builders of the environment and above; the API refuses anyone else.
 */
export function AccessExplain({ app, env }: Props) {
  const [open, setOpen] = useState(false);
  return (
    <details onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>Why can this person open it?</summary>
      {open ? <Body app={app} env={env} /> : null}
    </details>
  );
}

function Body({ app, env }: Props) {
  const { api } = useRouteContext({ from: '/_authed/apps/$appId' });
  const [picked, setPicked] = useState<Match | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [answer, setAnswer] = useState<{ readonly who: Match; readonly result: AccessExplained } | null>(null);
  const latest = useRef(0);
  const envTitle = ENV_TITLE[env.name].toLowerCase();

  useEffect(
    () => () => {
      latest.current += 1;
    },
    [],
  );

  function pick(match: Match | null) {
    latest.current += 1;
    setPicked(match);
    setAnswer(null);
    setError(null);
    setBusy(false);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!picked || busy) return;
    const request = ++latest.current;
    setBusy(true);
    setError(null);
    setAnswer(null);
    try {
      const result = await explainAccess(api, app.id, env.id, picked.id);
      if (request === latest.current) setAnswer({ who: picked, result });
    } catch (e) {
      if (request === latest.current) setError(e);
    } finally {
      if (request === latest.current) setBusy(false);
    }
  }

  const r = answer?.result;
  return (
    <form className="stack" onSubmit={submit} aria-label={`Explain access to ${ENV_TITLE[env.name]}`}>
      <Lookup
        label="Person: email or usr_ id"
        placeholder="name@example.com"
        idPattern={USER_ID_PATTERN}
        search={(email) => findAnyone(api, email)}
        hint="Only an org admin can look a person up by email. Anyone else types the person's usr_ id."
        picked={picked}
        onPick={pick}
      />
      <div className="toolbar">
        <Button type="submit" variant="primary" disabled={!picked || busy}>
          Explain
        </Button>
      </div>
      {error ? <ProblemNotice error={error} /> : null}
      {answer && r ? (
        <div className="stack" role="status" aria-label="Access explained">
          <p>
            <Badge tone={r.allowed ? 'success' : 'danger'}>{r.allowed ? 'can open' : 'cannot open'}</Badge>{' '}
            {accessSentence(r, answer.who.label, `${envTitle} of ${app.slug}`, envTitle)}
          </p>
          <dl className="facts">
            <dt>Best role granted</dt>
            <dd>{r.role ?? 'None'}</dd>
            <dt>Least role {envTitle} takes</dt>
            <dd>{r.floor}</dd>
            <dt>Person</dt>
            <dd>
              <code>{r.user_id}</code>
            </dd>
          </dl>
          {r.grants.length > 0 ? (
            <Table
              caption={`Grants that decided access to ${ENV_TITLE[env.name]}`}
              columns={GRANT_COLUMNS}
              rows={r.grants}
              rowKey={(g) => g.grant_id}
              empty="No grant decided it."
            />
          ) : null}
          <p className="muted">
            Worked out from the current sharing rules; the gateway follows them from the next
            snapshot (newest published: {r.published_version ?? 'none yet'}).
          </p>
        </div>
      ) : null}
    </form>
  );
}
