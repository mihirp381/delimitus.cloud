import { useRouteContext } from '@tanstack/react-router';
import { useId, useState } from 'react';
import {
  ceilingText,
  type Connection,
  type ConnectionChange,
  KIND_TITLE,
  needsCeiling,
  PREVIEW_WHO_TITLE,
  previewAccessOf,
  subjectMatches,
} from '../api/connections';
import { findPeople, type Match, USER_ID_PATTERN } from '../api/directory';
import { Lookup } from '../app-detail/Lookup';
import { Badge } from '../components/Badge';
import { Id } from '../components/Copy';
import { Person } from '../components/Person';
import { ChangeForm, EditablePanel } from './Editable';
import { ceilingOf, ClassificationField, PreviewAccessFields } from './fields';
import { same } from './save';

type Props = { readonly connection: Connection };

export function Overview({ connection: c }: Props) {
  return (
    <EditablePanel
      title="Overview"
      view={
        <dl className="facts">
          <dt>Owner</dt>
          <dd>
            <Person name={c.owner_name} email={c.owner_email} fallback="None" />
          </dd>
          <dt>Kind</dt>
          <dd>{KIND_TITLE[c.kind]}</dd>
          <dt>Classification</dt>
          <dd>{c.classification}</dd>
          <dt>Apps may be shared with</dt>
          <dd>{ceilingText(c)}</dd>
          <dt>Preview access</dt>
          <dd>
            {c.preview_access.who === 'group'
              ? `Members of ${c.preview_access_group_name ?? 'a group'}`
              : PREVIEW_WHO_TITLE[c.preview_access.who]}
          </dd>
          <dt>Setup</dt>
          <dd>
            <Badge tone={c.setup_status === 'ready' ? 'success' : 'warning'}>{c.setup_status}</Badge>
          </dd>
          <dt>Id</dt>
          <dd>
            <Id value={c.id} label="connection id" />
          </dd>
        </dl>
      }
      form={(close) => <OverviewForm connection={c} onClose={close} />}
    />
  );
}

function OverviewForm({ connection: c, onClose }: Props & { readonly onClose: () => void }) {
  const { api } = useRouteContext({ from: '/_authed' });
  const [owner, setOwner] = useState<Match | null>(null);
  const [classification, setClassification] = useState(c.classification);
  const [previewWho, setPreviewWho] = useState(c.preview_access.who);
  const [previewGroup, setPreviewGroup] = useState<Match[]>(
    c.preview_access.group_id ? [{ id: c.preview_access.group_id, label: c.preview_access_group_name ?? c.preview_access.group_id }] : [],
  );
  const [setup, setSetup] = useState(c.setup_status);
  const setupId = useId();

  function build(): ConnectionChange {
    const patch: ConnectionChange = {};
    if (owner && owner.id !== c.owner_user_id) patch.owner_user_id = owner.id;
    if (classification !== c.classification) {
      patch.classification = classification;
      if (needsCeiling(classification)) patch.ceiling = ceilingOf(c.ceiling.audience, subjectMatches(c));
    }
    const preview = previewAccessOf(previewWho, previewGroup[0]?.id ?? '');
    if (!same(preview, previewAccessOf(c.preview_access.who, c.preview_access.group_id ?? ''))) patch.preview_access = preview;
    if (setup !== c.setup_status) patch.setup_status = setup;
    return patch;
  }

  return (
    <ChangeForm label={`Change ${c.name}`} connection={c} build={build} onClose={onClose}>
      <Lookup
        label="Owner: email or usr_ id"
        placeholder="name@example.com"
        idPattern={USER_ID_PATTERN}
        search={(email) => findPeople(api, email)}
        hint={<>Now {c.owner_name ?? 'no one'}. Leave empty to keep.</>}
        picked={owner}
        onPick={setOwner}
      />
      <ClassificationField value={classification} onChange={setClassification} />
      <PreviewAccessFields who={previewWho} group={previewGroup} onWho={setPreviewWho} onGroup={setPreviewGroup} />
      <label className="field" htmlFor={setupId}>
        <span>Setup</span>
        <select id={setupId} value={setup} onChange={(e) => setSetup(e.target.value as Connection['setup_status'])}>
          <option value="pending">pending: credentials not set up yet</option>
          <option value="ready">ready: the runbook&apos;s first read worked</option>
        </select>
      </label>
    </ChangeForm>
  );
}
