import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useRouteContext } from '@tanstack/react-router';
import { type FormEvent, useId, useState } from 'react';
import {
  checksText,
  connectRepository,
  disconnectRepository,
  parseChecks,
  readRepository,
  type RepoLink,
  REPOSITORY_PATTERN,
  type RequiredCheck,
} from '../api/github';
import type { AppOut } from '../api/lifecycle';
import { ApiProblem } from '../api/problem';
import { Button } from '../components/Button';
import { ConfirmAction } from '../components/ConfirmAction';
import { Dialog } from '../components/Dialog';
import { ProblemNotice } from '../components/ProblemNotice';

export function repositoryKey(appId: string) {
  return ['github', appId] as const;
}

/**
 * The GitHub repository whose pushes deploy preview, and the checks promote requires on prod.
 * Connecting, changing and disconnecting need a builder on prod; the API refuses anyone else.
 * A repository the org's GitHub App cannot reach is refused with instructions in the problem.
 */
export function Repository({ app }: { readonly app: AppOut }) {
  const { api } = useRouteContext({ from: '/_authed/apps/$appId' });
  const queryClient = useQueryClient();
  const link = useQuery({
    queryKey: repositoryKey(app.id),
    queryFn: ({ signal }) => readRepository(api, app.id, signal),
  });
  const [notice, setNotice] = useState<string | null>(null);

  function saved(next: RepoLink | null, message: string) {
    queryClient.setQueryData(repositoryKey(app.id), next);
    setNotice(message);
  }

  return (
    <section className="panel" aria-labelledby="repo-heading">
      <h2 id="repo-heading">Repository</h2>
      {notice ? (
        <p className="notice notice-success" role="status">
          {notice}
        </p>
      ) : null}
      {link.isPending ? (
        <p className="muted">Loading the repository…</p>
      ) : link.isError ? (
        link.error instanceof ApiProblem && link.error.status === 403 ? (
          <p className="muted">Only builders of this app can see its repository.</p>
        ) : (
          <ProblemNotice error={link.error} />
        )
      ) : link.data === null ? (
        <>
          <p className="muted">
            No repository is connected. Connect one and every push to its branch deploys preview;
            production still changes only through promote.
          </p>
          <div className="toolbar">
            <RepositoryDialog app={app} current={null} onSaved={saved} />
          </div>
        </>
      ) : (
        <>
          <dl className="facts">
            <dt>Repository</dt>
            <dd>
              <code>{link.data.repository}</code>
            </dd>
            <dt>Branch</dt>
            <dd>
              <code>{link.data.branch}</code>
            </dd>
            <dt>Checks promote requires</dt>
            <dd>
              {link.data.required_checks.length === 0 ? (
                'None'
              ) : (
                <ul className="plain-list">
                  {link.data.required_checks.map((c) => (
                    <li key={`${c.workflow}:${c.name}`}>
                      {c.name} <code className="muted">{c.workflow}</code>
                    </li>
                  ))}
                </ul>
              )}
            </dd>
            <dt>Check each push reports</dt>
            <dd>{link.data.check_name}</dd>
            <dt>Changed</dt>
            <dd>
              <time dateTime={link.data.updated_at}>
                {new Date(link.data.updated_at).toLocaleString()}
              </time>
            </dd>
          </dl>
          <div className="toolbar">
            <RepositoryDialog app={app} current={link.data} onSaved={saved} />
            <ConfirmAction
              label="Disconnect"
              accessibleLabel="Disconnect the repository"
              title="Disconnect the repository"
              confirmText={app.slug}
              confirmLabel="Disconnect"
              onConfirm={async () => {
                await disconnectRepository(api, app.id);
                saved(null, `Disconnected ${link.data?.repository ?? 'the repository'}.`);
              }}
            >
              <p>
                Pushes to <code>{link.data.repository}</code> stop deploying preview of{' '}
                <strong>{app.slug}</strong>, and promote stops checking its required checks. What
                is deployed stays.
              </p>
            </ConfirmAction>
          </div>
        </>
      )}
    </section>
  );
}

interface DialogProps {
  readonly app: AppOut;
  readonly current: RepoLink | null;
  readonly onSaved: (next: RepoLink, message: string) => void;
}

function RepositoryDialog({ app, current, onSaved }: DialogProps) {
  const [open, setOpen] = useState(false);
  const title = current ? 'Change the repository' : 'Connect a repository';
  return (
    <>
      <Button variant={current ? 'secondary' : 'primary'} onClick={() => setOpen(true)}>
        {current ? 'Change' : 'Connect a repository'}
      </Button>
      <Dialog open={open} title={title} onClose={() => setOpen(false)}>
        {open ? (
          <RepositoryForm
            app={app}
            current={current}
            title={title}
            onClose={() => setOpen(false)}
            onSaved={(next, message) => {
              onSaved(next, message);
              setOpen(false);
            }}
          />
        ) : null}
      </Dialog>
    </>
  );
}

function RepositoryForm({
  app,
  current,
  title,
  onClose,
  onSaved,
}: DialogProps & { readonly title: string; readonly onClose: () => void }) {
  const { api } = useRouteContext({ from: '/_authed/apps/$appId' });
  const [repository, setRepository] = useState(current?.repository ?? '');
  const [branch, setBranch] = useState(current?.branch ?? '');
  const [checks, setChecks] = useState(checksText(current?.required_checks ?? []));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const repoId = useId();
  const branchId = useId();
  const checksId = useId();
  const typed = repository.trim();
  const ready = REPOSITORY_PATTERN.test(typed);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready || busy) return;
    setError(null);
    let required: RequiredCheck[];
    try {
      required = parseChecks(checks);
    } catch (e) {
      setError(e);
      return;
    }
    setBusy(true);
    try {
      const next = await connectRepository(api, app.id, {
        repository: typed,
        branch: branch.trim() || null,
        required_checks: required,
      });
      onSaved(next, `Connected ${next.repository}; pushes to ${next.branch} deploy preview.`);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="stack" onSubmit={submit} aria-label={title}>
      <label className="field" htmlFor={repoId}>
        <span>Repository, as owner/name</span>
        <input
          id={repoId}
          value={repository}
          placeholder="acme/expenses"
          spellCheck={false}
          autoComplete="off"
          onChange={(e) => setRepository(e.target.value)}
        />
      </label>
      <label className="field" htmlFor={branchId}>
        <span>Branch, empty for the repository&apos;s default branch</span>
        <input
          id={branchId}
          value={branch}
          placeholder="main"
          spellCheck={false}
          autoComplete="off"
          onChange={(e) => setBranch(e.target.value)}
        />
      </label>
      <label className="field" htmlFor={checksId}>
        <span>Checks promote requires, one per line: the workflow file, then the check name</span>
        <textarea
          id={checksId}
          rows={3}
          value={checks}
          placeholder=".github/workflows/ci.yml test"
          spellCheck={false}
          onChange={(e) => setChecks(e.target.value)}
        />
      </label>
      <p className="muted">
        The repository must be reachable through your company&apos;s GitHub App installation.
      </p>
      {error ? <ProblemNotice error={error} /> : null}
      <div className="actions">
        <Button onClick={onClose}>Cancel</Button>
        <Button type="submit" variant="primary" disabled={!ready || busy}>
          {current ? 'Save' : 'Connect'}
        </Button>
      </div>
    </form>
  );
}
