import { createFileRoute } from '@tanstack/react-router';
import { type FormEvent, useId, useState } from 'react';
import {
  allowHost,
  type CatalogueEntry,
  type EgressHost,
  type EgressHosts,
  removeHost,
} from '../../api/egress';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { ConfirmAction } from '../../components/ConfirmAction';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';

export const Route = createFileRoute('/_authed/egress')({
  component: EgressPage,
});

function EgressPage() {
  const { queries, queryClient, isAdmin } = Route.useRouteContext();
  const me = queries.useQuery('get', '/v1/whoami');
  const egress = queries.useQuery('get', '/v1/egress');
  const [notice, setNotice] = useState<string | null>(null);
  const admin = isAdmin(me.data);

  /** Puts the list the API answered with in place, keeping the IP and proxy as they were. */
  async function saved(update: EgressHosts, message: string) {
    const key = queries.queryOptions('get', '/v1/egress').queryKey;
    if (egress.data) queryClient.setQueryData(key, { ...egress.data, hosts: update.hosts });
    setNotice(message);
    await queryClient.invalidateQueries({
      queryKey: queries.queryOptions('get', '/v1/egress/catalogue').queryKey,
    });
  }

  return (
    <>
      <div className="page-head">
        <h1>Internet access</h1>
      </div>
      <p className="muted">
        Apps reach the internet only through your cell&apos;s egress proxy, and only the hosts listed
        here. Partners see every call come from one fixed IP.
      </p>
      {egress.isPending ? (
        <p className="muted">Loading…</p>
      ) : egress.isError ? (
        <ProblemNotice error={egress.error} />
      ) : (
        <>
          <OutboundIp ip={egress.data.outbound_ip} />
          {notice ? (
            <p className="notice notice-success" role="status">
              {notice}
            </p>
          ) : null}
          <Hosts hosts={egress.data.hosts} admin={admin} onSaved={saved} />
          {me.isPending ? null : admin ? (
            <AddHost onSaved={saved} />
          ) : (
            <p className="muted">Only org admins can add or remove hosts.</p>
          )}
        </>
      )}
    </>
  );
}

function OutboundIp({ ip }: { readonly ip: string | null }) {
  const [copied, setCopied] = useState<string | null>(null);

  async function copy(value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied('Copied.');
    } catch {
      setCopied('Could not copy; select the address instead.');
    }
  }

  return (
    <section className="panel" aria-labelledby="egress-ip-heading">
      <h2 id="egress-ip-heading">Fixed outbound IP</h2>
      {ip ? (
        <>
          <p>
            Your apps reach the internet from <code>{ip}</code>. Give it to a partner whose firewall
            only lets known addresses in.
          </p>
          <div className="toolbar">
            <Button onClick={() => void copy(ip)}>Copy IP</Button>
            {copied ? (
              <span className="muted" role="status">
                {copied}
              </span>
            ) : null}
          </div>
        </>
      ) : (
        <p className="muted">
          Your cell has not told us its fixed outbound IP yet. It is created with the egress proxy,
          when the first host is allowed; if it stays missing, ask your operator.
        </p>
      )}
    </section>
  );
}

interface HostsProps {
  readonly hosts: readonly EgressHost[];
  readonly admin: boolean;
  readonly onSaved: (update: EgressHosts, message: string) => Promise<void>;
}

function Hosts({ hosts, admin, onSaved }: HostsProps) {
  const { api } = Route.useRouteContext();
  const columns: Column<EgressHost>[] = [
    {
      header: 'Host',
      cell: (h) => (
        <>
          <code>{h.host}</code> {h.high_risk ? <Badge tone="warning">high risk</Badge> : null}
        </>
      ),
    },
    {
      header: 'Added',
      cell: (h) => <time dateTime={h.created_at}>{new Date(h.created_at).toLocaleString()}</time>,
    },
    {
      header: 'Added by',
      cell: (h) =>
        h.added_by_user_id ? (
          <code>{h.added_by_user_id}</code>
        ) : h.approval_request_id ? (
          <>
            Approval <code>{h.approval_request_id}</code>
          </>
        ) : (
          'SSC'
        ),
    },
  ];
  if (admin) {
    columns.push({
      header: 'Action',
      className: 'actions-cell',
      cell: (h) => (
        <ConfirmAction
          label="Remove"
          accessibleLabel={`Remove ${h.host}`}
          title={`Remove ${h.host}`}
          confirmText={h.host}
          confirmLabel="Remove host"
          onConfirm={async () => onSaved(await removeHost(api, h.host), `Removed ${h.host}.`)}
        >
          <p>
            Apps stop reaching <code>{h.host}</code>; connections already open to it close once the proxy reads the change.
          </p>
        </ConfirmAction>
      ),
    });
  }
  return (
    <section className="panel" aria-labelledby="egress-hosts-heading">
      <h2 id="egress-hosts-heading">Allowed hosts</h2>
      <Table
        caption="Hosts apps may reach"
        columns={columns}
        rows={hosts}
        rowKey={(h) => h.host}
        empty="No host is allowed yet: apps cannot reach the internet."
      />
    </section>
  );
}

function AddHost({ onSaved }: { readonly onSaved: HostsProps['onSaved'] }) {
  const { api, queries } = Route.useRouteContext();
  const catalogue = queries.useQuery('get', '/v1/egress/catalogue');
  const [host, setHost] = useState('');
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const hostId = useId();
  const ackId = useId();
  const typed = host.trim().toLowerCase();
  const entry = catalogue.data?.entries.find((e) => e.host === typed) ?? null;
  const highRisk = entry?.high_risk ?? false;

  function type(value: string) {
    setHost(value);
    setAcknowledged(false);
    setError(null);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!typed || busy || (highRisk && !acknowledged)) return;
    setBusy(true);
    setError(null);
    try {
      await onSaved(await allowHost(api, typed, acknowledged), `Allowed ${typed}.`);
      type('');
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  const columns: readonly Column<CatalogueEntry>[] = [
    {
      header: 'Host',
      cell: (e) => (
        <>
          <code>{e.host}</code> {e.high_risk ? <Badge tone="warning">high risk</Badge> : null}
        </>
      ),
    },
    { header: 'For', cell: (e) => e.purpose },
    { header: 'Note', cell: (e) => e.note || null },
    {
      header: 'Action',
      className: 'actions-cell',
      cell: (e) =>
        e.listed ? (
          <span className="muted">Allowed</span>
        ) : (
          <Button onClick={() => type(e.host)} aria-label={`Pick ${e.host}`}>
            Pick
          </Button>
        ),
    },
  ];

  return (
    <section className="panel" aria-labelledby="egress-add-heading">
      <h2 id="egress-add-heading">Allow a host</h2>
      <form className="stack" onSubmit={submit} aria-label="Allow a host">
        <label className="field" htmlFor={hostId}>
          <span>Host, such as api.example.com or *.example.com</span>
          <input
            id={hostId}
            value={host}
            spellCheck={false}
            autoComplete="off"
            onChange={(e) => type(e.target.value)}
          />
        </label>
        {highRisk ? (
          <div className="notice notice-warning" role="note">
            <p>
              <strong>{typed}</strong> is high risk: {entry?.note || 'anyone can put data there.'}
            </p>
            <label className="check" htmlFor={ackId}>
              <input
                id={ackId}
                type="checkbox"
                checked={acknowledged}
                onChange={(e) => setAcknowledged(e.target.checked)}
              />
              <span>Apps may send company data to {typed}, and I accept that</span>
            </label>
          </div>
        ) : null}
        {error ? <ProblemNotice error={error} /> : null}
        <div>
          <Button type="submit" variant="primary" disabled={!typed || busy || (highRisk && !acknowledged)}>
            Allow host
          </Button>
        </div>
      </form>
      <h3>Common hosts</h3>
      {catalogue.isPending ? (
        <p className="muted">Loading the catalogue…</p>
      ) : catalogue.isError ? (
        <ProblemNotice error={catalogue.error} />
      ) : (
        <Table
          caption="Common hosts to pick from"
          columns={columns}
          rows={catalogue.data.entries}
          rowKey={(e) => e.host}
          empty="The catalogue is empty."
        />
      )}
    </section>
  );
}
