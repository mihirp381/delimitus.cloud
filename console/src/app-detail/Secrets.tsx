import { useRouteContext } from '@tanstack/react-router';
import { useState } from 'react';
import type { components } from '../api/schema';
import type { AppOut, EnvironmentOut } from '../api/lifecycle';
import { Badge } from '../components/Badge';
import { Button } from '../components/Button';
import { ProblemNotice } from '../components/ProblemNotice';
import { type Column, Table } from '../components/Table';
import { ENV_TITLE } from './names';

type Secret = components['schemas']['SecretOut'];

interface Props {
  readonly app: AppOut;
  readonly env: EnvironmentOut;
}

const PATH = '/v1/apps/{app_id}/environments/{environment_id}/secrets' as const;

/**
 * `ssc secret set`, as `packages/ssc_cli/src/ssc_cli/commands/secret.py` takes it: the app, the
 * secret's name, and `--env`, which has no default. The value never goes on the command line:
 * the CLI asks for it at a hidden prompt, or reads it from stdin.
 */
export function secretCommands(slug: string, env: EnvironmentOut['name']) {
  const set = `ssc secret set ${slug} NAME --env ${env}`;
  return { prompt: set, piped: `${set} < value.txt` };
}

const COLUMNS: readonly Column<Secret>[] = [
  { header: 'Name', cell: (s) => <code>{s.name}</code> },
  { header: 'Latest version', cell: (s) => s.version },
  {
    header: 'Live version',
    cell: (s) =>
      s.live_version === s.version ? (
        s.live_version
      ) : (
        <>
          {s.live_version ?? 'None'} <Badge tone="warning">takes effect on the next deployment</Badge>
        </>
      ),
  },
  { header: 'Updated', cell: (s) => <time dateTime={s.updated_at}>{new Date(s.updated_at).toLocaleString()}</time> },
];

/**
 * An environment's secrets, read only once the section is opened: names and versions, never a
 * value, which nothing in SSC hands back. Setting and rotating stay in the CLI, so the section
 * shows the commands. Builders of the environment and above; the API refuses anyone else.
 */
export function Secrets({ app, env }: Props) {
  const [open, setOpen] = useState(false);
  return (
    <details onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>Secrets</summary>
      {open ? <SecretsBody app={app} env={env} /> : null}
    </details>
  );
}

function SecretsBody({ app, env }: Props) {
  const { queries } = useRouteContext({ from: '/_authed/apps/$appId' });
  const list = queries.useQuery('get', PATH, { params: { path: { app_id: app.id, environment_id: env.id } } });
  const where = ENV_TITLE[env.name];
  const commands = secretCommands(app.slug, env.name);
  return (
    <div className="stack">
      {list.isPending ? (
        <p className="muted">Loading secrets…</p>
      ) : list.isError ? (
        <ProblemNotice error={list.error} />
      ) : (
        <Table
          caption={`Secrets of ${where}`}
          columns={COLUMNS}
          rows={list.data.items}
          rowKey={(s) => s.name}
          empty={`${where} of ${app.slug} has no secrets.`}
          emptyHint="Set one from a terminal with the command below."
        />
      )}
      <p className="muted">
        Values are never shown anywhere: not here, not in the CLI and not by the API. Only the
        app&apos;s own {where.toLowerCase()} environment can read them.
      </p>
      <h3>Set or rotate a secret</h3>
      <p>
        From a terminal, signed in with <code>ssc login</code>. Put the secret&apos;s name in place
        of <code>NAME</code>: upper-case A-Z, 0-9 and _, at most 64 characters, and also the
        environment variable the app reads. The CLI asks for the value at a hidden prompt.
      </p>
      <Command label={`Set a secret in ${where.toLowerCase()}`} text={commands.prompt} />
      <p>Or pipe the value in from a file or a password manager, so that it is never typed:</p>
      <Command label={`Set a secret in ${where.toLowerCase()} from a file`} text={commands.piped} />
      <p className="muted">
        Setting a name that already exists stores its next version, which is how you rotate one.
        SSC then deploys {where.toLowerCase()} again to put it live; add <code>--wait</code> to
        wait for that. Only an org admin, the app&apos;s owner or a builder on{' '}
        {where.toLowerCase()} can set one, and never an agent.
      </p>
    </div>
  );
}

/** One command to paste into a terminal, with a button that copies exactly it. */
function Command({ label, text }: { readonly label: string; readonly text: string }) {
  const [copied, setCopied] = useState<string | null>(null);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied('Copied.');
    } catch {
      setCopied('Could not copy; select the command instead.');
    }
  }

  return (
    <div className="command">
      <pre aria-label={label}>
        <code>{text}</code>
      </pre>
      <Button onClick={() => void copy()} aria-label={`Copy the command: ${label}`}>
        Copy
      </Button>
      {copied ? (
        <span className="muted" role="status">
          {copied}
        </span>
      ) : null}
    </div>
  );
}
