import type { Connection } from '../api/connections';
import { AdminOnly } from '../components/AdminOnly';
import { Badge } from '../components/Badge';
import { ConfirmAction } from '../components/ConfirmAction';
import { Panel } from '../components/Panel';
import { useConnectionChange } from './save';

export function Status({ connection }: { readonly connection: Connection }) {
  const save = useConnectionChange(connection.name);
  const { name } = connection;
  const active = connection.status === 'active';
  return (
    <Panel title="Status">
      <p>
        {active ? (
          <>
            <Badge tone="success">active</Badge> Linked apps can query it. Suspending stops every query on it, for
            every app, until it is resumed; nothing is deleted.
          </>
        ) : (
          <>
            <Badge tone="danger">suspended</Badge> Every query on it is refused. Resuming lets the apps linked to it
            query it again.
          </>
        )}
      </p>
      <AdminOnly quiet>
        <div className="actions">
          {active ? (
            <ConfirmAction
              label="Suspend this connection"
              title={`Suspend ${name}`}
              confirmText={name}
              confirmLabel="Suspend"
              onConfirm={() =>
                save(
                  { status: 'suspended' },
                  `Suspended ${name}. Apps linked to it lose it within a few seconds: every query on it is refused until it is resumed.`,
                )
              }
            >
              <p>
                Every app linked to <code>{name}</code> loses it within a few seconds: the data gateway refuses
                their queries from its next snapshot until you resume the connection. Its links, limits and
                credentials stay as they are.
              </p>
            </ConfirmAction>
          ) : (
            <ConfirmAction
              variant="secondary"
              label="Resume this connection"
              title={`Resume ${name}`}
              confirmLabel="Resume"
              onConfirm={() =>
                save({ status: 'active' }, `Resumed ${name}. Apps linked to it can query it again within a few seconds.`)
              }
            >
              <p>
                Apps linked to <code>{name}</code> can query it again from the data gateway&apos;s next snapshot.
              </p>
            </ConfirmAction>
          )}
        </div>
      </AdminOnly>
    </Panel>
  );
}
