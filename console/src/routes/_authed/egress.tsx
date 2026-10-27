import { createFileRoute, Link } from '@tanstack/react-router';
import { type FormEvent, useId, useState } from 'react';
import {
  allowHost,
  type CatalogueEntry,
  type EgressHost,
  type EgressHosts,
  removeHost,
} from '../../api/egress';
import { Ago } from '../../components/Ago';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { ConfirmAction } from '../../components/ConfirmAction';
import { Id } from '../../components/Copy';
import { Loading } from '../../components/Loading';
import { Notice } from '../../components/Notice';
import { PageHeader } from '../../components/PageHeader';
import { Person } from '../../components/Person';
import { Panel } from '../../components/Panel';
import { ProblemNotice } from '../../components/ProblemNotice';
import { type Column, Table } from '../../components/Table';
import { useToast } from '../../components/Toast';

export const Route = createFileRoute('/_authed/egress')({
  component: EgressPage,
});

function EgressPage() {
  const { queries, queryClient, isAdmin } = Route.useRouteContext();
  const me = queries.useQuery('get', '/v1/whoami');
  const egress = queries.useQuery('get', '/v1/egress');
  const toast = useToast();
  const admin = isAdmin(me.data);

  /** Puts the list the API answered with in place, keeping the IP and proxy as they were. */
  async function saved(update: EgressHosts, message: string) {
    const key = queries.queryOptions('get', '/v1/egress').queryKey;
    if (egress.data) queryClient.setQueryData(key, { ...egress.data, hosts: update.hosts });
    toast(message);
    await queryClient.invalidateQueries({
      queryKey: queries.queryOptions('get', '/v1/egress/catalogue').queryKey,
    });
  }

  return (
    <>
      <PageHeader
        title="Internet access"
        purpose="Apps reach the internet only through your cell's egress proxy, and only the hosts listed here."
        about={<p>Partners see every call come from the fixed outbound IP; give it to a partner whose firewall only lets known addresses in.</p>}
      />
      {egress.isPending ? (
        <Loading label="Loading internet access…" rows={3} />
      ) : egress.isError ? (
        <ProblemNotice error={egress.error} />
      ) : (
        <>
          <OutboundIp ip={egress.data.outbound_ip} />
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
  return (
    <dl className="panel facts">
      <dt>Fixed outbound IP</dt>
      <dd>
        {ip ? (
          <Id value={ip} label="fixed outbound IP" />
        ) : (
          <span className="muted">
            Not set yet. It is created with the egress proxy, when the first host is allowed; if it stays missing,
            ask your operator.
          </span>
        )}
      </dd>
    </dl>
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
      cell: (h) => <Ago at={h.created_at} />,
    },
    {
      header: 'Added by',
      cell: (h) => (
        <>
          <Person name={h.added_by_name} email={h.added_by_email} fallback="SSC" />
          {h.approval_request_id ? (
            <>
              {' '}
              <Link to="/approvals/$approvalId" params={{ approvalId: h.approval_request_id }}>
                by approval
              </Link>
            </>
          ) : null}
        </>
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
    <Panel title="Allowed hosts">
      <Table
        caption="Hosts apps may reach"
        columns={columns}
        rows={hosts}
        rowKey={(h) => h.host}
        empty="No host is allowed yet: apps cannot reach the internet."
        emptyHint={
          admin
            ? 'Allow a host below, by name or from the common ones.'
            : 'Ask an org admin to allow the hosts your app calls.'
        }
      />
    </Panel>
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
  const hintId = useId();
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
    <Panel title="Allow a host">
      <form className="stack" onSubmit={submit} aria-label="Allow a host">
        <label className="field" htmlFor={hostId}>
          <span>Host, such as api.example.com or *.example.com</span>
          <input
            id={hostId}
            aria-describedby={hintId}
            value={host}
            spellCheck={false}
            autoComplete="off"
            onChange={(e) => type(e.target.value)}
          />
        </label>
        <p id={hintId} className="muted">
          A wildcard under a shared host reaches every tenant of it; prefer the exact host, such as
          &lt;bucket&gt;.s3.amazonaws.com.
        </p>
        {highRisk ? (
          <Notice tone="warn" role="note">
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
          </Notice>
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
        <Loading label="Loading the catalogue…" rows={3} />
      ) : catalogue.isError ? (
        <ProblemNotice error={catalogue.error} />
      ) : (
        <Table
          caption="Common hosts to pick from"
          columns={columns}
          rows={catalogue.data.entries}
          rowKey={(e) => e.host}
          empty="The catalogue is empty."
          emptyHint="Type the host you need in the field above."
        />
      )}
    </Panel>
  );
}
