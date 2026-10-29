import { createFileRoute, Link } from '@tanstack/react-router';
import { type FormEvent, type ReactNode, useId, useState } from 'react';
import { when } from '../../api/cell';
import {
  type AppMove,
  type Delegation,
  type Domain,
  DOMAIN_POLL_MS,
  flipDomain,
  flipped,
  polling,
  requestDomain,
} from '../../api/domain';
import { ApiProblem } from '../../api/problem';
import { AdminOnly } from '../../components/AdminOnly';
import { Badge, type Tone } from '../../components/Badge';
import { Button } from '../../components/Button';
import { ConfirmAction } from '../../components/ConfirmAction';
import { Id } from '../../components/Copy';
import { Loading } from '../../components/Loading';
import { Notice } from '../../components/Notice';
import { PageHeader } from '../../components/PageHeader';
import { Panel } from '../../components/Panel';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';
import { useToast } from '../../components/Toast';
import { copyMessage, useClipboard } from '../../components/useClipboard';
import { itTicket, PROVIDER_NAME, parentZone, providerOf, providerSteps, recordLines } from '../../domain/providers';

export const Route = createFileRoute('/_authed/domain')({
  component: DomainPage,
});

const TITLE = 'Your own address';

const ADMISSION_RULES =
  'Use a name at least three labels long under a domain your company owns, such as tools.example.com, that no other organisation uses.';

function DomainPage() {
  return (
    <AdminOnly title={TITLE} refusal="Only org admins can set the address your apps live at.">
      <YourAddress />
    </AdminOnly>
  );
}

function stateBadge(domain: Domain): readonly [Tone, string] | null {
  if (flipped(domain)) return ['success', 'In use'];
  switch (domain.delegation?.state) {
    case 'pending_dns':
      return ['warning', 'Waiting for your DNS change'];
    case 'issuing':
      return ['info', 'Issuing certificate'];
    case 'active':
      return ['success', 'Active'];
    case 'failed':
      return ['danger', 'Failed'];
    case 'retiring':
      return ['neutral', 'Being removed'];
    default:
      return null;
  }
}

function YourAddress() {
  const { queries } = Route.useRouteContext();
  const domain = queries.useQuery('get', '/v1/domain', undefined, {
    refetchInterval: (q) => (polling(q.state.data) ? DOMAIN_POLL_MS : false),
  });
  const badge = domain.data ? stateBadge(domain.data) : null;
  return (
    <>
      <PageHeader
        title={TITLE}
        purpose="Serve your apps under your company's own domain instead of ours"
        status={badge ? <Badge tone={badge[0]}>{badge[1]}</Badge> : null}
      />
      {domain.isPending ? (
        <Loading label="Loading your address…" rows={3} />
      ) : domain.isError ? (
        <ProblemNotice error={domain.error} />
      ) : (
        <Address domain={domain.data} />
      )}
    </>
  );
}

function useSaved() {
  const { queries, queryClient } = Route.useRouteContext();
  const toast = useToast();
  const key = queries.queryOptions('get', '/v1/domain').queryKey;
  return {
    saved(domain: Domain, message: string) {
      queryClient.setQueryData(key, domain);
      toast(message);
    },
    reread() {
      void queryClient.invalidateQueries({ queryKey: key });
    },
  };
}

function Address({ domain }: { readonly domain: Domain }) {
  const d = domain.delegation;
  let body: ReactNode;
  if (flipped(domain)) body = <Moved domain={domain} />;
  else if (!d || d.state === 'retired') body = <RequestPanel />;
  else if (d.state === 'pending_dns') body = <Records delegation={d} />;
  else if (d.state === 'issuing') body = <Issuing delegation={d} />;
  else if (d.state === 'active') body = <Ready domain={domain} delegation={d} />;
  else if (d.state === 'failed') body = <Failed delegation={d} />;
  else
    body = (
      <Notice tone="info">
        <p>
          Your operator is removing <code>{d.suffix}</code>.
        </p>
      </Notice>
    );
  return (
    <>
      <dl className="panel facts">
        <dt>Apps live under</dt>
        <dd>
          <code>{domain.current_suffix}</code>
        </dd>
        {flipped(domain)
          ? null
          : domain.previous.map((p) => (
              <Redirect key={p.suffix} suffix={p.suffix} until={p.until} />
            ))}
      </dl>
      {body}
    </>
  );
}

function Redirect({ suffix, until }: { readonly suffix: string; readonly until: string }) {
  return (
    <>
      <dt>Old addresses</dt>
      <dd>
        Old addresses under <code>{suffix}</code> redirect until <time dateTime={until}>{when(until)}</time>
      </dd>
    </>
  );
}

function CopyText({ value, label, what, children }: { readonly value: string; readonly label: string; readonly what: string; readonly children: ReactNode }) {
  const { state, copy } = useClipboard(1600);
  return (
    <>
      <Button onClick={() => void copy(value)} aria-label={label}>
        {children}
      </Button>
      {state !== 'idle' ? (
        <span className="muted" role="status">
          {copyMessage(state, what)}
        </span>
      ) : null}
    </>
  );
}

function suggestion(email: string | null | undefined, mode: string | undefined): string {
  const at = email?.lastIndexOf('@') ?? -1;
  if (mode !== 'directory' || !email || at < 0) return '';
  return `tools.${email.slice(at + 1).toLowerCase()}`;
}

function RequestPanel() {
  const { api, queries } = Route.useRouteContext();
  const me = queries.useQuery('get', '/v1/whoami');
  const { saved, reread } = useSaved();
  const [suffix, setSuffix] = useState(() => suggestion(me.data?.email, me.data?.sign_in_mode));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const inputId = useId();
  const typed = suffix.trim().toLowerCase().replace(/\.$/, '');
  const code = error instanceof ApiProblem ? error.code : undefined;
  const malformed = code === 'VALIDATION_FAILED';
  const refused = malformed || Boolean(code?.startsWith('DOMAIN_SUFFIX_'));

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!typed || busy) return;
    setBusy(true);
    setError(null);
    try {
      saved(await requestDomain(api, typed), `Asked for ${typed}.`);
    } catch (e) {
      setError(e);
      if (e instanceof ApiProblem && e.code === 'ALREADY_EXISTS') reread();
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="Use your own address">
      <p>One DNS change by your IT team, usually live within an hour; we do the rest.</p>
      <form className="stack" onSubmit={submit} aria-label="Create your own address">
        <label className="field" htmlFor={inputId}>
          <span>Subdomain to delegate</span>
          <input
            id={inputId}
            value={suffix}
            placeholder="tools.example.com"
            spellCheck={false}
            autoComplete="off"
            aria-invalid={refused || undefined}
            onChange={(e) => {
              setSuffix(e.target.value);
              setError(null);
            }}
          />
        </label>
        {error ? <ProblemNotice error={error} /> : null}
        {malformed ? <p className="muted">{ADMISSION_RULES}</p> : null}
        <Notice tone="warn" role="note">
          <p>
            Apps under your domain share it with your other sites: an app there can set cookies and receive forms for
            the whole domain. Use a dedicated sub-label such as <code>tools.</code>, or a domain kept only for these
            apps.
          </p>
          <p>
            The form targets on each app&apos;s environment card, under <Link to="/">Apps</Link>, list the only other
            hosts its forms may post to.
          </p>
        </Notice>
        <div>
          <Button type="submit" variant="primary" disabled={!typed || busy}>
            Create address
          </Button>
        </div>
      </form>
    </Panel>
  );
}

function observedText(observed: Delegation['observed_ns']): string[] {
  const seen = new Set<string>();
  for (const answer of Object.values(observed ?? {})) for (const ns of answer) seen.add(ns);
  return [...seen].sort();
}

function SettingUp({ suffix }: { readonly suffix: string }) {
  return (
    <Panel title="Setting up the zone…">
      <p>
        We are making the zone for <code>{suffix}</code>. The NS records for your IT team appear here within a minute.
      </p>
    </Panel>
  );
}

function Records({ delegation }: { readonly delegation: Delegation }) {
  if (delegation.name_servers.length === 0) return <SettingUp suffix={delegation.suffix} />;
  return <NameServers delegation={delegation} />;
}

function NameServers({ delegation }: { readonly delegation: Delegation }) {
  const { suffix, name_servers: nameServers } = delegation;
  const zone = parentZone(suffix);
  const provider = providerOf(delegation.provider);
  const ticket = itTicket(suffix, nameServers);
  const ticketId = useId();
  const seen = observedText(delegation.observed_ns);
  const columns: readonly Column<string>[] = [
    { header: 'Name', cell: () => <code>{suffix}</code> },
    { header: 'Type', cell: () => <code>NS</code> },
    { header: 'Value', cell: (ns) => <Id value={ns} label={`name server ${ns}`} /> },
  ];
  return (
    <>
      <Panel title="Waiting for your DNS change">
        <p>
          Ask your IT team to add these {nameServers.length} NS records in the zone <code>{zone}</code>.
        </p>
        <Table
          caption="NS records to add"
          columns={columns}
          rows={nameServers}
          rowKey={(ns) => ns}
          empty="No name servers yet."
          emptyHint="Reload the page in a minute."
        />
        <div className="toolbar">
          <CopyText value={recordLines(suffix, nameServers)} label="Copy all records" what="records">
            Copy all
          </CopyText>
        </div>
        <h3>What we see from the internet right now</h3>
        {seen.length === 0 ? (
          <p>Nothing yet.</p>
        ) : (
          <ul className="plain-list" aria-label="Name servers seen for your subdomain">
            {seen.map((ns) => (
              <li key={ns}>
                <code>{ns}</code>
              </li>
            ))}
          </ul>
        )}
        <p className="muted">We check every minute and move on by ourselves once every resolver sees the records.</p>
      </Panel>
      <Panel title="Ticket for your IT team">
        <label className="field" htmlFor={ticketId}>
          <span>Ready to paste</span>
          <textarea id={ticketId} readOnly rows={ticket.split('\n').length + 1} value={ticket} />
        </label>
        <div className="toolbar">
          <CopyText value={ticket} label="Copy the ticket" what="ticket">
            Copy
          </CopyText>
        </div>
      </Panel>
      <Panel title={`Steps in ${PROVIDER_NAME[provider]}`}>
        <ol className="plain-list">
          {providerSteps(provider, suffix).map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
      </Panel>
    </>
  );
}

function Issuing({ delegation }: { readonly delegation: Delegation }) {
  return (
    <Panel title="Issuing certificate">
      <p>
        Your DNS change for <code>{delegation.suffix}</code> is in. Issuing certificate, usually under 30 minutes,
        sometimes up to 2 hours.
      </p>
      {delegation.failure ? <p className="muted">Certificate authority says: {delegation.failure}</p> : null}
    </Panel>
  );
}

function useFlip() {
  const { api } = Route.useRouteContext();
  const { saved, reread } = useSaved();
  return async (to: 'customer' | 'default', message: string) => {
    try {
      saved(await flipDomain(api, to), message);
    } catch (e) {
      reread();
      throw e;
    }
  };
}

function Ready({ domain, delegation }: { readonly domain: Domain; readonly delegation: Delegation }) {
  const flip = useFlip();
  const { suffix } = delegation;
  const { rolling } = domain;
  const columns: readonly Column<AppMove>[] = [
    { header: 'App', cell: (a) => a.slug },
    { header: 'Production address', cell: (a) => <code>{a.hosts[0]}</code> },
    { header: 'Preview address', cell: (a) => <code>{a.hosts[1]}</code> },
  ];
  return (
    <Panel title="Active">
      <p>
        <code>{suffix}</code> is ready. Moving changes every app&apos;s address at once.
      </p>
      {rolling > 0 ? (
        <Notice tone="info">
          <p>
            {rolling} {rolling === 1 ? 'app is' : 'apps are'} still being prepared; you can move once they are ready.
          </p>
        </Notice>
      ) : null}
      <Table
        caption="Apps that move"
        columns={columns}
        rows={domain.apps_that_move}
        rowKey={(a) => a.slug}
        empty="No app yet."
        emptyHint={`New apps live under ${suffix} once you move.`}
      />
      <div className="toolbar">
        <ConfirmAction
          label={`Move my apps to ${suffix}`}
          title={`Move my apps to ${suffix}`}
          confirmLabel="Move my apps"
          variant="secondary"
          disabled={!domain.ready_to_flip}
          onConfirm={() => flip('customer', `Your apps now live under ${suffix}.`)}
        >
          <p>
            Every app moves to its address under <code>{suffix}</code> at once, and everyone signs in again once. The
            old addresses under <code>{domain.current_suffix}</code> redirect for 30 days.
          </p>
        </ConfirmAction>
      </div>
    </Panel>
  );
}

function Moved({ domain }: { readonly domain: Domain }) {
  const flip = useFlip();
  const back = domain.default_suffix;
  return (
    <Panel title="Your apps live under your address">
      <dl className="facts">
        <dt>Current address</dt>
        <dd>
          <code>{domain.current_suffix}</code>
        </dd>
        {domain.previous.map((p) => (
          <Redirect key={p.suffix} suffix={p.suffix} until={p.until} />
        ))}
      </dl>
      <div className="toolbar">
        <ConfirmAction
          label={`Move back to ${back}`}
          title={`Move back to ${back}`}
          confirmLabel="Move back"
          variant="secondary"
          onConfirm={() => flip('default', `Your apps now live under ${back}.`)}
        >
          <p>
            Every app moves back to its address under <code>{back}</code> at once, and everyone signs in again once.
            The addresses under <code>{domain.current_suffix}</code> redirect for 30 days.
          </p>
        </ConfirmAction>
      </div>
    </Panel>
  );
}

function Failed({ delegation }: { readonly delegation: Delegation }) {
  const { api } = Route.useRouteContext();
  const { saved } = useSaved();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function retry() {
    setBusy(true);
    setError(null);
    try {
      saved(await requestDomain(api, delegation.suffix), `Asked for ${delegation.suffix} again.`);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="Could not set up your address">
      <Notice tone="warn">
        <p>
          <code>{delegation.suffix}</code>: {delegation.failure ?? 'the setup failed.'}
        </p>
      </Notice>
      {error ? <ProblemNotice error={error} /> : null}
      <div className="toolbar">
        <Button variant="primary" disabled={busy} onClick={() => void retry()}>
          Try again
        </Button>
      </div>
    </Panel>
  );
}
