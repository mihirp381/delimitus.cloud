import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { type Grants, withGrant } from '../src/api/grants';
import { openTab } from './appPage';
import { type Call, type Handler, json, problem } from './fakeApi';
import { signedIn, start, toast } from './harness';

const OWNER = 'usr_cccccccccccccccccccc';
const PERSON = 'usr_pppppppppppppppppppp';
const GROUP = 'grp_ffffffffffffffffffff';
const APP_ID = 'app_aaaaaaaaaaaaaaaaaaaa';
const PROD = 'env_prod0000000000000000';
const PREVIEW = 'env_preview0000000000000';
const RUN = 'kil_rrrrrrrrrrrrrrrrrrrr';
const DELETE_RUN = 'kil_dddddddddddddddddddd';
const OP = 'dep_oooooooooooooooooooo';

type Status = 'active' | 'disabled' | 'quarantined' | 'deleting';

function app(status: Status = 'active', owner = OWNER) {
  return {
    id: APP_ID,
    slug: 'expenses',
    owner_user_id: owner,
    owner_state: 'active',
    status,
    created_at: '2026-09-28T10:00:00Z',
    environments: [
      { id: PREVIEW, name: 'preview', config_version: 1, form_targets: [], grants_version: 0, current_deployment_id: null, url: null },
      { id: PROD, name: 'prod', config_version: 2, form_targets: [], grants_version: 3, current_deployment_id: 'dep_eeeeeeeeeeeeeeeeeeee', url: null },
    ],
  };
}

const APP_PATH = `/v1/apps/${APP_ID}`;
const PROD_GRANTS = `${APP_PATH}/environments/${PROD}/grants`;
const PREVIEW_GRANTS = `${APP_PATH}/environments/${PREVIEW}/grants`;
const ORG_USER = { id: 'gnt_000000000000000000o1', role: 'user', subject_kind: 'org', subject_id: null, subject_name: null, subject_email: null } as const;

function grants(env: string, version: number, list: Grants['grants'] = []): Grants {
  return { environment_id: env, grants_version: version, grants: list };
}

function whoami(role: 'admin' | 'member' | null): Handler {
  return () =>
    json(200, {
      org_id: 'org_ffffffffffffffffffff',
      subject: OWNER,
      kind: 'user',
      credential_id: 'c',
      is_agent: false,
      client_id: null,
      role,
    });
}

/** The first render in a worker can be slow while modules load under parallel test files. */
const FIRST_RENDER_MS = 5000;

const NEVER: Handler = () => new Promise<Response>(() => undefined);

/** A handler whose response waits until `answer` is called. */
function held(): { handler: Handler; answer: (r: Response) => void } {
  let answer: (r: Response) => void = () => undefined;
  const handler: Handler = () =>
    new Promise<Response>((resolve) => {
      answer = resolve;
    });
  return { handler, answer: (r) => answer(r) };
}

const FINANCE = () => json(200, { groups: [{ id: GROUP, name: 'Finance', member_count: 4 }] });

function page(extra: Record<string, Handler | Handler[]> = {}, status: Status = 'active') {
  return {
    [`GET ${APP_PATH}`]: () => json(200, app(status)),
    [`GET ${PROD_GRANTS}`]: () => json(200, grants(PROD, 3, [ORG_USER])),
    [`GET ${PREVIEW_GRANTS}`]: () => json(200, grants(PREVIEW, 0)),
    ...extra,
  };
}

function echoGrants(env: string, version: number): Handler {
  return ({ body }: Call) =>
    json(
      200,
      grants(
        env,
        version,
        ((body as { grants: Grants['grants'] }).grants ?? []).map((g, i) => ({
          ...g,
          subject_id: g.subject_id ?? null,
          id: `gnt_00000000000000000${String(i).padStart(3, '0')}`,
        })),
      ),
    );
}

const SHARED = [
  { role: 'user', subject_kind: 'org' },
  { role: 'user', subject_kind: 'group', subject_id: GROUP },
];
const WIDEN = { kind: 'widen_audience', connection: null, approval_id: null, state: null };
const EXCEED = { kind: 'exceed_ceiling', connection: 'finance', approval_id: null, state: null };

/** The requirements check answering `list` for prod at version 3. */
function required(list: readonly object[]): Handler {
  return () => json(200, { environment_id: PROD, grants_version: 3, requirements: list });
}

async function openShare(envName: 'Production' | 'Preview') {
  const panel = await screen.findByRole('region', { name: 'Who has access' }, { timeout: FIRST_RENDER_MS });
  await waitFor(() => expect(within(panel).queryByText('Loading access…')).toBeNull());
  fireEvent.click(within(panel).getByRole('button', { name: `Share ${envName}` }));
  const dialog = await screen.findByRole('dialog', { name: `Share ${envName.toLowerCase()}` });
  return { panel, dialog };
}

async function submitShare(dialog: HTMLElement) {
  await act(async () => {
    fireEvent.click(within(dialog).getByRole('button', { name: 'Share' }));
  });
}

describe('share dialog', () => {
  it('finds a group by name and shares production with it through If-Match', async () => {
    const { api } = start(
      `/apps/${APP_ID}?tab=access`,
      page({
        'GET /v1/groups': () => json(200, { groups: [{ id: GROUP, name: 'Finance', member_count: 4 }] }),
        [`PUT ${PROD_GRANTS}`]: echoGrants(PROD, 4),
      }),
      signedIn(),
    );
    const { panel, dialog } = await openShare('Production');
    fireEvent.change(within(dialog).getByLabelText('Group name or grp_ id'), { target: { value: 'finance' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Find' }));
    });
    expect(api.of('GET', '/v1/groups')[0]?.query.get('name')).toBe('finance');
    const match = await within(dialog).findByRole('radio', { name: /Finance/ });
    expect((match as HTMLInputElement).checked).toBe(true);
    expect(within(dialog).getByText(/4 active members/)).toBeTruthy();
    await submitShare(dialog);
    const put = api.of('PUT', PROD_GRANTS)[0];
    expect(put?.headers.get('If-Match')).toBe('"3"');
    expect(put?.body).toEqual({
      grants: [
        { role: 'user', subject_kind: 'org' },
        { role: 'user', subject_kind: 'group', subject_id: GROUP },
      ],
    });
    expect(await toast()).toBe('Shared production with Finance (user).');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(within(panel).getByText(GROUP)).toBeTruthy();
  });

  it('lets an admin find a person by email and replaces the role they had', async () => {
    const had = { id: 'gnt_000000000000000000u1', role: 'user', subject_kind: 'user', subject_id: PERSON, subject_name: 'Olive Owner', subject_email: 'olive@example.com' } as const;
    const { api } = start(
      `/apps/${APP_ID}?tab=access`,
      page({
        [`GET ${PROD_GRANTS}`]: () => json(200, grants(PROD, 3, [had])),
        'GET /v1/users': () =>
          json(200, {
            users: [
              { id: PERSON, display_name: 'Pat', email: 'pat@example.com', role: 'member', status: 'active' },
              { id: 'usr_qqqqqqqqqqqqqqqqqqqq', display_name: 'Pat (old)', email: 'pat@example.com', role: 'member', status: 'deactivated' },
            ],
          }),
        [`PUT ${PROD_GRANTS}`]: echoGrants(PROD, 4),
      }),
      signedIn(),
    );
    const { dialog } = await openShare('Production');
    fireEvent.click(within(dialog).getByRole('radio', { name: 'A person' }));
    fireEvent.change(within(dialog).getByLabelText('Email or usr_ id'), { target: { value: 'pat@example.com' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Find' }));
    });
    expect(api.of('GET', '/v1/users')[0]?.query.get('email')).toBe('pat@example.com');
    const old = await within(dialog).findByRole('radio', { name: /Pat \(old\)/ });
    expect((old as HTMLInputElement).disabled).toBe(true);
    expect((within(dialog).getByRole('radio', { name: /^Pat usr_p/ }) as HTMLInputElement).checked).toBe(true);
    fireEvent.change(within(dialog).getByLabelText('Role'), { target: { value: 'builder' } });
    await submitShare(dialog);
    expect(api.of('PUT', PROD_GRANTS)[0]?.body).toEqual({
      grants: [{ role: 'builder', subject_kind: 'user', subject_id: PERSON }],
    });
  });

  it.each([
    ['member', whoami('member')],
    ['no role (agent or deactivated)', whoami(null)],
  ])('offers a %s only a usr_ id, never the email search', async (_, me) => {
    const { api } = start(
      `/apps/${APP_ID}?tab=access`,
      page({ 'GET /v1/whoami': me, [`PUT ${PROD_GRANTS}`]: echoGrants(PROD, 4) }),
      signedIn(),
    );
    const { dialog } = await openShare('Production');
    fireEvent.click(within(dialog).getByRole('radio', { name: 'A person' }));
    expect(within(dialog).getByText(/Only an org admin can look people up by email/)).toBeTruthy();
    expect(within(dialog).queryByRole('button', { name: 'Find' })).toBeNull();
    const share = within(dialog).getByRole('button', { name: 'Share' }) as HTMLButtonElement;
    fireEvent.change(within(dialog).getByLabelText('User id'), { target: { value: 'pat@example.com' } });
    expect(share.disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText('User id'), { target: { value: PERSON } });
    expect(share.disabled).toBe(false);
    await submitShare(dialog);
    expect(api.of('PUT', PROD_GRANTS)[0]?.body).toEqual({
      grants: [
        { role: 'user', subject_kind: 'org' },
        { role: 'user', subject_kind: 'user', subject_id: PERSON },
      ],
    });
    expect(api.of('GET', '/v1/users')).toHaveLength(0);
  });

  it('waits for whoami before offering the email search', async () => {
    start(`/apps/${APP_ID}?tab=access`, page({ 'GET /v1/whoami': NEVER }), signedIn());
    const { dialog } = await openShare('Production');
    fireEvent.click(within(dialog).getByRole('radio', { name: 'A person' }));
    expect(within(dialog).getByText('Checking your role…')).toBeTruthy();
    expect(within(dialog).queryByRole('button', { name: 'Find' })).toBeNull();
    expect(within(dialog).getByLabelText('User id')).toBeTruthy();
  });

  it('offers only the builder role on preview', async () => {
    start(`/apps/${APP_ID}?tab=access&env=preview`, page(), signedIn());
    const { dialog } = await openShare('Preview');
    const options = within(within(dialog).getByLabelText('Role')).getAllByRole('option');
    expect(options.map((o) => (o as HTMLOptionElement).value)).toEqual(['builder']);
  });

  it('re-reads after a 412 and applies the change to the version it read', async () => {
    const { api } = start(
      `/apps/${APP_ID}?tab=access`,
      page({
        [`GET ${PROD_GRANTS}`]: [
          () => json(200, grants(PROD, 3)),
          () => json(200, grants(PROD, 4, [ORG_USER]), { ETag: '"4"' }),
        ],
        [`PUT ${PROD_GRANTS}`]: [() => problem(412, 'PRECONDITION_STALE', 'Changed since you read it'), echoGrants(PROD, 5)],
      }),
      signedIn(),
    );
    const { dialog } = await openShare('Production');
    fireEvent.change(within(dialog).getByLabelText('Group name or grp_ id'), { target: { value: GROUP } });
    await submitShare(dialog);
    const puts = api.of('PUT', PROD_GRANTS);
    expect(puts.map((p) => p.headers.get('If-Match'))).toEqual(['"3"', '"4"']);
    expect(puts[0]?.body).toEqual({ grants: [{ role: 'user', subject_kind: 'group', subject_id: GROUP }] });
    expect(puts[1]?.body).toEqual({
      grants: [
        { role: 'user', subject_kind: 'org' },
        { role: 'user', subject_kind: 'group', subject_id: GROUP },
      ],
    });
    expect(await toast()).toBe(`Shared production with ${GROUP} (user).`);
  });

  it('sends nothing when the subject already has that role', async () => {
    const { api } = start(`/apps/${APP_ID}?tab=access`, page(), signedIn());
    const { dialog } = await openShare('Production');
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Everyone in the organisation' }));
    await submitShare(dialog);
    expect(api.of('PUT', PROD_GRANTS)).toHaveLength(0);
    expect(await toast()).toBeTruthy();
  });

  it('says a 202 waits for approval and changes nothing', async () => {
    start(
      `/apps/${APP_ID}?tab=access`,
      page({
        [`PUT ${PROD_GRANTS}`]: () =>
          json(202, { environment_id: PROD, grants_version: 3, approval_ids: ['apr_aaaaaaaaaaaaaaaaaaaa'] }, { ETag: '"3"' }),
      }),
      signedIn(),
    );
    const { panel, dialog } = await openShare('Production');
    fireEvent.change(within(dialog).getByLabelText('Group name or grp_ id'), { target: { value: GROUP } });
    await submitShare(dialog);
    expect((await within(panel).findByRole('status')).textContent).toBe(
      'Waiting for approval, nothing changed yet: apr_aaaaaaaaaaaaaaaaaaaa.',
    );
    expect(within(panel).queryByText(GROUP)).toBeNull();
  });

  it('explains APPROVAL_REQUIRED and asks for approval of exactly that change', async () => {
    const { api } = start(
      `/apps/${APP_ID}?tab=access`,
      page({
        [`PUT ${PROD_GRANTS}`]: () => problem(409, 'APPROVAL_REQUIRED', 'This change needs an approval first.'),
        [`POST ${PROD_GRANTS}/requirements`]: required([WIDEN]),
        'POST /v1/approvals': ({ body }) =>
          json(201, { id: 'apr_bbbbbbbbbbbbbbbbbbbb', state: 'pending', ...(body as object) }),
      }),
      signedIn(),
    );
    const { dialog } = await openShare('Production');
    fireEvent.change(within(dialog).getByLabelText('Group name or grp_ id'), { target: { value: GROUP } });
    await submitShare(dialog);
    expect((await within(dialog).findByRole('alert')).textContent).toContain('APPROVAL_REQUIRED');
    const note = await within(dialog).findByRole('note');
    expect(note.textContent).toContain('This change needs approval before it applies');
    expect(note.textContent).toContain('another org admin');
    const check = api.of('POST', `${PROD_GRANTS}/requirements`)[0];
    expect(check?.body).toEqual({ grants: SHARED });
    expect(check?.headers.get('If-Match')).toBeNull();
    expect(api.of('POST', '/v1/approvals')).toHaveLength(0);
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Ask for approval' }));
    });
    const ask = api.of('POST', '/v1/approvals')[0];
    expect(ask?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(ask?.body).toEqual({ environment_id: PROD, kind: 'widen_audience', payload: { grants: SHARED } });
    expect(api.of('POST', '/v1/approvals')).toHaveLength(1);
    const status = await within(dialog).findByRole('status');
    expect([...status.querySelectorAll('a')].map((a) => a.getAttribute('href'))).toEqual(['/approvals/apr_bbbbbbbbbbbbbbbbbbbb']);
    expect(status.textContent).toContain('widen the audience');
    expect(within(dialog).queryByRole('note')).toBeNull();
  });

  it('asks the connection owner when the change passes a ceiling, with the grants it was refused for', async () => {
    const { api } = start(
      `/apps/${APP_ID}?tab=access`,
      page({
        [`PUT ${PROD_GRANTS}`]: () => problem(409, 'APPROVAL_REQUIRED', 'This change needs an approval first.'),
        [`POST ${PROD_GRANTS}/requirements`]: required([EXCEED]),
        'POST /v1/approvals': ({ body }) =>
          json(201, { id: 'apr_cccccccccccccccccccc', state: 'pending', ...(body as object) }),
      }),
      signedIn(),
    );
    const { dialog } = await openShare('Production');
    fireEvent.change(within(dialog).getByLabelText('Group name or grp_ id'), { target: { value: GROUP } });
    await submitShare(dialog);
    expect((await within(dialog).findByRole('note')).textContent).toContain('finance');
    const reads = api.of('GET', PROD_GRANTS).length;
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Ask for approval' }));
    });
    expect(api.of('POST', '/v1/approvals').map((c) => c.body)).toEqual([
      { environment_id: PROD, kind: 'exceed_ceiling', payload: { connection: 'finance', grants: SHARED } },
    ]);
    // The desired grants, not the ones in force: the approval must match the refused PUT.
    expect(api.of('GET', PROD_GRANTS)).toHaveLength(reads);
    const status = await within(dialog).findByRole('status');
    expect([...status.querySelectorAll('a')].map((a) => a.getAttribute('href'))).toEqual(['/approvals/apr_cccccccccccccccccccc']);
    expect(status.textContent).toContain('finance');
  });

  it('asks for every approval the change needs', async () => {
    let n = 0;
    const { api } = start(
      `/apps/${APP_ID}?tab=access`,
      page({
        [`PUT ${PROD_GRANTS}`]: () => problem(409, 'APPROVAL_REQUIRED', 'This change needs an approval first.'),
        [`POST ${PROD_GRANTS}/requirements`]: required([WIDEN, EXCEED]),
        'POST /v1/approvals': ({ body }) => {
          n += 1;
          return json(201, { id: `apr_${String(n).repeat(20)}`, state: 'pending', ...(body as object) });
        },
      }),
      signedIn(),
    );
    const { dialog } = await openShare('Production');
    fireEvent.change(within(dialog).getByLabelText('Group name or grp_ id'), { target: { value: GROUP } });
    await submitShare(dialog);
    await within(dialog).findByRole('note');
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Ask for approval' }));
    });
    expect(api.of('POST', '/v1/approvals').map((c) => c.body)).toEqual([
      { environment_id: PROD, kind: 'widen_audience', payload: { grants: SHARED } },
      { environment_id: PROD, kind: 'exceed_ceiling', payload: { connection: 'finance', grants: SHARED } },
    ]);
    const items = within(await within(dialog).findByRole('status')).getAllByRole('listitem');
    expect(items.map((i) => i.querySelector('a')?.getAttribute('href'))).toEqual([
      '/approvals/apr_11111111111111111111',
      '/approvals/apr_22222222222222222222',
    ]);
  });

  it('does not ask again for an approval already pending, and shows it as pending', async () => {
    const pending = { ...WIDEN, approval_id: 'apr_dddddddddddddddddddd', state: 'pending' };
    const { api } = start(
      `/apps/${APP_ID}?tab=access`,
      page({
        [`PUT ${PROD_GRANTS}`]: () => problem(409, 'APPROVAL_REQUIRED', 'This change needs an approval first.'),
        [`POST ${PROD_GRANTS}/requirements`]: required([pending, EXCEED]),
        'POST /v1/approvals': ({ body }) =>
          json(201, { id: 'apr_eeeeeeeeeeeeeeeeeeee', state: 'pending', ...(body as object) }),
      }),
      signedIn(),
    );
    const { dialog } = await openShare('Production');
    fireEvent.change(within(dialog).getByLabelText('Group name or grp_ id'), { target: { value: GROUP } });
    await submitShare(dialog);
    const note = await within(dialog).findByRole('note');
    expect(within(note).getByRole('link', { name: 'pending request' }).getAttribute('href')).toBe('/approvals/apr_dddddddddddddddddddd');
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Ask for approval' }));
    });
    expect(api.of('POST', '/v1/approvals').map((c) => (c.body as { kind: string }).kind)).toEqual(['exceed_ceiling']);
    const items = within(await within(dialog).findByRole('status')).getAllByRole('listitem');
    expect(items.map((i) => [i.querySelector('a')?.getAttribute('href'), i.textContent?.endsWith('(pending)')])).toEqual([
      ['/approvals/apr_dddddddddddddddddddd', true],
      ['/approvals/apr_eeeeeeeeeeeeeeeeeeee', true],
    ]);
  });

  it('asks for a whole email address before searching people', async () => {
    const { api } = start(`/apps/${APP_ID}?tab=access`, page(), signedIn());
    const { dialog } = await openShare('Production');
    fireEvent.click(within(dialog).getByRole('radio', { name: 'A person' }));
    fireEvent.change(within(dialog).getByLabelText('Email or usr_ id'), { target: { value: 'pat' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Find' }));
    });
    expect((await within(dialog).findByRole('alert')).textContent).toContain('Type a whole email address');
    expect(api.of('GET', '/v1/users')).toHaveLength(0);
  });

  it('gives a subject one grant, replacing the role it had', () => {
    const builder = { role: 'builder', subject_kind: 'org' } as const;
    const owner = { id: 'gnt_000000000000000000b1', role: 'builder', subject_kind: 'user', subject_id: OWNER, subject_name: 'Olive Owner', subject_email: 'olive@example.com' } as const;
    expect(withGrant(builder)([ORG_USER, owner])).toEqual([owner, builder]);
  });

  it('drops a late search result once a grp_ id was pasted', async () => {
    const other = 'grp_gggggggggggggggggggg';
    const search = held();
    const { api } = start(
      `/apps/${APP_ID}?tab=access`,
      page({ 'GET /v1/groups': search.handler, [`PUT ${PROD_GRANTS}`]: echoGrants(PROD, 4) }),
      signedIn(),
    );
    const { dialog } = await openShare('Production');
    const box = within(dialog).getByLabelText('Group name or grp_ id');
    fireEvent.change(box, { target: { value: 'finance' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Find' }));
    await waitFor(() => expect(api.of('GET', '/v1/groups')).toHaveLength(1));
    fireEvent.change(box, { target: { value: other } });
    await act(async () => {
      search.answer(FINANCE());
    });
    expect(within(dialog).queryByRole('radio', { name: /Finance/ })).toBeNull();
    await submitShare(dialog);
    expect(api.of('PUT', PROD_GRANTS)[0]?.body).toEqual({
      grants: [
        { role: 'user', subject_kind: 'org' },
        { role: 'user', subject_kind: 'group', subject_id: other },
      ],
    });
  });

  it('drops a group search that lands after switching to a person', async () => {
    const search = held();
    const { api } = start(`/apps/${APP_ID}?tab=access`, page({ 'GET /v1/groups': search.handler }), signedIn());
    const { dialog } = await openShare('Production');
    fireEvent.change(within(dialog).getByLabelText('Group name or grp_ id'), { target: { value: 'finance' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Find' }));
    await waitFor(() => expect(api.of('GET', '/v1/groups')).toHaveLength(1));
    fireEvent.click(within(dialog).getByRole('radio', { name: 'A person' }));
    await act(async () => {
      search.answer(FINANCE());
    });
    expect((within(dialog).getByRole('button', { name: 'Share' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('drops a search that lands after the dialog closed', async () => {
    const search = held();
    const { api } = start(`/apps/${APP_ID}?tab=access`, page({ 'GET /v1/groups': search.handler }), signedIn());
    const { panel, dialog } = await openShare('Production');
    fireEvent.change(within(dialog).getByLabelText('Group name or grp_ id'), { target: { value: 'finance' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Find' }));
    await waitFor(() => expect(api.of('GET', '/v1/groups')).toHaveLength(1));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await act(async () => {
      search.answer(FINANCE());
    });
    fireEvent.click(within(panel).getByRole('button', { name: 'Share Production' }));
    const again = await screen.findByRole('dialog', { name: 'Share production' });
    expect((within(again).getByRole('button', { name: 'Share' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it.each([
    ['the role', (dialog: HTMLElement) => fireEvent.change(within(dialog).getByLabelText('Role'), { target: { value: 'builder' } })],
    [
      'the subject',
      (dialog: HTMLElement) =>
        fireEvent.change(within(dialog).getByLabelText('Group name or grp_ id'), {
          target: { value: 'grp_gggggggggggggggggggg' },
        }),
    ],
  ])('forgets an APPROVAL_REQUIRED refusal when %s changes', async (_, edit) => {
    const { api } = start(
      `/apps/${APP_ID}?tab=access`,
      page({
        [`PUT ${PROD_GRANTS}`]: () => problem(409, 'APPROVAL_REQUIRED', 'This change needs an approval first.'),
        [`POST ${PROD_GRANTS}/requirements`]: required([WIDEN]),
      }),
      signedIn(),
    );
    const { dialog } = await openShare('Production');
    fireEvent.change(within(dialog).getByLabelText('Group name or grp_ id'), { target: { value: GROUP } });
    await submitShare(dialog);
    expect(await within(dialog).findByRole('note')).toBeTruthy();
    edit(dialog);
    expect(within(dialog).queryByRole('note')).toBeNull();
    expect(within(dialog).queryByRole('alert')).toBeNull();
    expect(within(dialog).queryByRole('button', { name: 'Ask for approval' })).toBeNull();
    expect(api.of('POST', '/v1/approvals')).toHaveLength(0);
  });

  it('keeps the dialog open on a refusal', async () => {
    start(
      `/apps/${APP_ID}?tab=access`,
      page({ [`PUT ${PROD_GRANTS}`]: () => problem(409, 'APP_NOT_ACTIVE', 'This app is not active.') }),
      signedIn(),
    );
    const { dialog } = await openShare('Production');
    fireEvent.change(within(dialog).getByLabelText('Group name or grp_ id'), { target: { value: GROUP } });
    await submitShare(dialog);
    expect((await within(dialog).findByRole('alert')).textContent).toContain('APP_NOT_ACTIVE');
    expect(within(dialog).queryByRole('note')).toBeNull();
  });
});

async function adminPanel() {
  await openTab('Settings');
  return screen.findByRole('region', { name: 'Admin actions' }, { timeout: FIRST_RENDER_MS });
}

async function confirmIn(dialog: HTMLElement, label: string) {
  fireEvent.change(within(dialog).getByLabelText(/Type/), { target: { value: 'expenses' } });
  await act(async () => {
    fireEvent.click(within(dialog).getByRole('button', { name: label }));
  });
}

function run(state: 'running' | 'completed' | 'failed', steps: number) {
  const names = ['gateway_deny', 'datagw_suspend', 'egress_remove', 'scale_to_zero', 'pause_timers'];
  return {
    run_id: RUN,
    app_id: APP_ID,
    mode: 'disable',
    state,
    started_at: '2026-09-29T10:00:00Z',
    finished_at: state === 'running' ? null : '2026-09-29T10:00:02Z',
    total_ms: state === 'running' ? null : 2000,
    steps: names.slice(0, steps).map((name, i) => ({
      name,
      state: state === 'running' && i === steps - 1 ? 'running' : 'done',
      snapshot_version: null,
      started_at: '2026-09-29T10:00:00Z',
      finished_at: null,
      elapsed_ms: state === 'running' && i === steps - 1 ? null : 100,
      attempts: 1,
      error: null,
    })),
  };
}

const DELETE_STEPS = [
  'gateway_deny',
  'datagw_suspend',
  'egress_remove',
  'scale_to_zero',
  'remove_timers',
  'drop_databases',
  'delete_files',
  'delete_secrets',
  'delete_services',
  'delete_bundles',
  'remove_records',
];

/** A removal with its first `steps` steps begun; a failed one failed at the last of them. */
function deleteRun(state: 'running' | 'completed' | 'failed', steps: number) {
  const last = (i: number) => i === steps - 1;
  return {
    run_id: DELETE_RUN,
    app_id: APP_ID,
    state,
    started_at: '2026-09-29T10:00:00Z',
    finished_at: state === 'running' ? null : '2026-09-29T10:00:09Z',
    total_ms: state === 'running' ? null : 9000,
    steps: DELETE_STEPS.slice(0, steps).map((name, i) => ({
      name,
      state: last(i) && state !== 'completed' ? state : 'done',
      snapshot_version: null,
      started_at: '2026-09-29T10:00:00Z',
      finished_at: null,
      elapsed_ms: last(i) && state === 'running' ? null : 100,
      attempts: last(i) && state === 'failed' ? 5 : 1,
      error: last(i) && state === 'failed' ? 'DATABASE_UNAVAILABLE' : null,
      removed: name === 'drop_databases' && !(last(i) && state === 'failed') ? 2 : null,
    })),
  };
}

describe('admin actions', () => {
  it('deletes after the slug is typed, sends the slug, and follows the run', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${APP_PATH}`]: [() => json(200, app('active')), () => json(200, app('deleting'))],
        [`POST ${APP_PATH}/delete`]: () => json(202, { run_id: DELETE_RUN, state: 'running' }),
        [`GET ${APP_PATH}/delete/${DELETE_RUN}`]: [
          () => json(200, deleteRun('running', 6)),
          () => json(200, deleteRun('completed', 11)),
        ],
      }),
      signedIn(),
    );
    const admin = await adminPanel();
    fireEvent.click(await within(admin).findByRole('button', { name: 'Delete app' }));
    const dialog = await screen.findByRole('dialog', { name: 'Delete expenses' });
    expect(dialog.textContent).toContain('This cannot be undone');
    const confirm = within(dialog).getByRole('button', { name: 'Delete' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText(/Type/), { target: { value: 'expense' } });
    expect(confirm.disabled).toBe(true);
    expect(api.of('POST', `${APP_PATH}/delete`)).toHaveLength(0);
    await confirmIn(dialog, 'Delete');
    const post = api.of('POST', `${APP_PATH}/delete`)[0];
    expect(post?.body).toEqual({ confirm: 'expenses' });
    expect(post?.headers.get('Idempotency-Key')).toBeTruthy();
    const progress = await within(admin).findByRole('group', { name: 'Delete run' });
    await within(progress).findByText('completed', {}, { timeout: 3000 });
    expect(within(progress).getByText('Drop databases')).toBeTruthy();
    expect(within(progress).getByText('Remove sharing, requests and links')).toBeTruthy();
    expect(within(progress).getByRole('status').textContent).toContain('The app is deleted');
    expect(within(progress).getByRole('link', { name: 'Open this run' }).getAttribute('href')).toBe(
      `/apps/${APP_ID}/delete/${DELETE_RUN}`,
    );
    // The app is being deleted from the moment the API answered: nothing else is offered.
    expect(await screen.findByText('deleting')).toBeTruthy();
    for (const name of ['Disable', 'Quarantine', 'Enable', 'Transfer ownership', 'Delete app']) {
      expect(within(admin).queryByRole('button', { name })).toBeNull();
    }
    expect(within(admin).getByRole('button', { name: 'Continue deleting' })).toBeTruthy();
    const polls = api.of('GET', `${APP_PATH}/delete/${DELETE_RUN}`).length;
    await new Promise((resolve) => setTimeout(resolve, 1500));
    expect(api.of('GET', `${APP_PATH}/delete/${DELETE_RUN}`)).toHaveLength(polls);
  });

  it('shows a refusal to delete in the dialog and reads the app again', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        [`POST ${APP_PATH}/delete`]: () =>
          problem(409, 'APP_OPERATION_IN_FLIGHT', 'A deploy of this app is still running.', 'Wait for it to finish, then retry.'),
      }),
      signedIn(),
    );
    const admin = await adminPanel();
    const reads = api.of('GET', APP_PATH).length;
    fireEvent.click(await within(admin).findByRole('button', { name: 'Delete app' }));
    const dialog = await screen.findByRole('dialog', { name: 'Delete expenses' });
    await confirmIn(dialog, 'Delete');
    const alert = await within(dialog).findByRole('alert');
    expect(alert.textContent).toContain('APP_OPERATION_IN_FLIGHT');
    expect(alert.textContent).toContain('Wait for it to finish');
    await waitFor(() => expect(api.of('GET', APP_PATH).length).toBeGreaterThan(reads));
    expect(within(admin).queryByRole('group', { name: 'Delete run' })).toBeNull();
  });

  it('offers a member no delete', async () => {
    start(`/apps/${APP_ID}`, page({ 'GET /v1/whoami': whoami('member') }), signedIn());
    const admin = await adminPanel();
    expect(await within(admin).findByText(/Only an org admin can .* delete this app/)).toBeTruthy();
    expect(within(admin).queryByRole('button', { name: 'Delete app' })).toBeNull();
  });

  it('shows a removal on its own page after the app is gone, with what each step removed', async () => {
    const { api } = start(
      `/apps/${APP_ID}/delete/${DELETE_RUN}`,
      {
        [`GET ${APP_PATH}`]: () => problem(404, 'NOT_FOUND', 'No such app.'),
        [`GET ${APP_PATH}/delete/${DELETE_RUN}`]: () => json(200, deleteRun('completed', 11)),
      },
      signedIn(),
    );
    await screen.findByRole('heading', { name: 'Delete run', level: 1 }, { timeout: FIRST_RENDER_MS });
    const view = await screen.findByRole('group', { name: 'Delete run' });
    expect(within(view).getByText('9000 ms (9.0 s)')).toBeTruthy();
    expect(within(view).getByText(DELETE_RUN)).toBeTruthy();
    expect(within(view).queryByRole('link', { name: 'Open this run' })).toBeNull();
    expect(within(view).getByRole('link', { name: 'Back to Apps' }).getAttribute('href')).toBe('/');
    const steps = within(view).getByRole('table', { name: 'Delete steps' });
    expect(within(steps).getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'Step',
      'State',
      'Started',
      'Finished',
      'Removed',
      'Took',
      'Tries',
      'Last error',
    ]);
    const rows = within(steps).getAllByRole('row');
    expect(rows).toHaveLength(12);
    expect(within(rows[6]!).getAllByRole('cell').map((c) => c.textContent)[4]).toBe('2');
    // The app's own page is gone, so the bar names it without a link to it.
    const crumbs = screen.getByRole('navigation', { name: 'Breadcrumb' });
    await waitFor(() => expect(api.of('GET', APP_PATH).length).toBeGreaterThan(0));
    await waitFor(() => expect(within(crumbs).queryByRole('link', { name: 'App' })).toBeNull());
    expect(within(crumbs).getByText('Delete run')).toBeTruthy();
  });

  it('says where a removal stopped and how to continue', async () => {
    start(
      `/apps/${APP_ID}/delete/${DELETE_RUN}`,
      {
        [`GET ${APP_PATH}`]: () => json(200, app('deleting')),
        [`GET ${APP_PATH}/delete/${DELETE_RUN}`]: () => json(200, deleteRun('failed', 6)),
      },
      signedIn(),
    );
    const view = await screen.findByRole('group', { name: 'Delete run' }, { timeout: FIRST_RENDER_MS });
    const notice = within(view).getByRole('status');
    expect(notice.textContent).toContain('The removal stopped at "Drop databases"');
    expect(notice.textContent).toContain('Delete the app again');
    expect(within(view).getByText('DATABASE_UNAVAILABLE')).toBeTruthy();
    expect((await screen.findByRole('link', { name: 'expenses' })).getAttribute('href')).toBe(`/apps/${APP_ID}`);
  });

  it('continues a removal that stopped', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page(
        {
          [`POST ${APP_PATH}/delete`]: () => json(202, { run_id: DELETE_RUN, state: 'running' }),
          [`GET ${APP_PATH}/delete/${DELETE_RUN}`]: () => json(200, deleteRun('completed', 11)),
        },
        'deleting',
      ),
      signedIn(),
    );
    const admin = await adminPanel();
    expect(await within(admin).findByText(/is being deleted/)).toBeTruthy();
    fireEvent.click(await within(admin).findByRole('button', { name: 'Continue deleting' }));
    await confirmIn(await screen.findByRole('dialog', { name: 'Delete expenses' }), 'Delete');
    expect(api.of('POST', `${APP_PATH}/delete`)[0]?.body).toEqual({ confirm: 'expenses' });
    const progress = await within(admin).findByRole('group', { name: 'Delete run' });
    await within(progress).findByText('completed', {}, { timeout: 3000 });
  });

  it('disables after the slug is typed and follows the run by the id in the body', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${APP_PATH}`]: [() => json(200, app('active')), () => json(200, app('disabled'))],
        [`POST ${APP_PATH}/kill-switch`]: () => json(202, { run_id: RUN, state: 'running' }),
        [`GET ${APP_PATH}/kill-switch/${RUN}`]: [() => json(200, run('running', 2)), () => json(200, run('completed', 5))],
      }),
      signedIn(),
    );
    const admin = await adminPanel();
    expect(within(admin).queryByRole('button', { name: 'Enable' })).toBeNull();
    fireEvent.click(await within(admin).findByRole('button', { name: 'Disable' }));
    const dialog = await screen.findByRole('dialog', { name: 'Disable expenses' });
    expect(api.of('POST', `${APP_PATH}/kill-switch`)).toHaveLength(0);
    await confirmIn(dialog, 'Disable');
    const post = api.of('POST', `${APP_PATH}/kill-switch`)[0];
    expect(post?.body).toEqual({ mode: 'disable' });
    expect(post?.headers.get('Idempotency-Key')).toBeTruthy();
    const progress = await within(admin).findByRole('group', { name: 'Kill switch run' });
    await waitFor(() => expect(within(progress).getAllByText('running').length).toBeGreaterThan(0));
    await within(progress).findByText('completed', {}, { timeout: 3000 });
    expect(within(progress).getByText('Pause timers')).toBeTruthy();
    expect(within(progress).getByText(/in 2000 ms/)).toBeTruthy();
    expect(within(progress).getByRole('link', { name: 'Open this run' }).getAttribute('href')).toBe(
      `/apps/${APP_ID}/kill-switch/${RUN}`,
    );
    expect(await screen.findByText('disabled')).toBeTruthy();
    expect(within(admin).getByRole('button', { name: 'Enable' })).toBeTruthy();
    expect(within(admin).getByRole('button', { name: 'Quarantine' })).toBeTruthy();
    expect(within(admin).queryByRole('button', { name: 'Disable' })).toBeNull();
    const polls = api.of('GET', `${APP_PATH}/kill-switch/${RUN}`).length;
    await new Promise((resolve) => setTimeout(resolve, 1500));
    expect(api.of('GET', `${APP_PATH}/kill-switch/${RUN}`)).toHaveLength(polls);
    await openTab('Overview');
    expect((screen.getByRole('button', { name: 'Roll back Production' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('shows a run on its own page with its mode, total and each step, and stops reading once it ends', async () => {
    const { api } = start(
      `/apps/${APP_ID}/kill-switch/${RUN}`,
      {
        [`GET ${APP_PATH}`]: () => json(200, app('disabled')),
        [`GET ${APP_PATH}/kill-switch/${RUN}`]: [() => json(200, run('running', 2)), () => json(200, run('completed', 5))],
      },
      signedIn(),
    );
    await screen.findByRole('heading', { name: 'Kill switch run', level: 1 }, { timeout: FIRST_RENDER_MS });
    const view = await screen.findByRole('group', { name: 'Kill switch run' });
    expect(within(view).getByText('Still running')).toBeTruthy();
    expect(within(view).queryByRole('link', { name: 'Open this run' })).toBeNull();
    await within(view).findByText('completed', {}, { timeout: 3000 });
    expect(within(view).getByText('Disable')).toBeTruthy();
    expect(within(view).getByText('2000 ms (2.0 s)')).toBeTruthy();
    expect(within(view).getByText(RUN)).toBeTruthy();
    const steps = within(view).getByRole('table', { name: 'Kill switch steps' });
    expect(within(steps).getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'Step',
      'State',
      'Started',
      'Finished',
      'Took',
      'Tries',
      'Last error',
    ]);
    expect(within(steps).getAllByRole('row')).toHaveLength(6);
    expect(within(steps).getAllByText('100 ms')).toHaveLength(5);
    expect((await screen.findByRole('link', { name: 'expenses' })).getAttribute('href')).toBe(`/apps/${APP_ID}`);
    const polls = api.of('GET', `${APP_PATH}/kill-switch/${RUN}`).length;
    await new Promise((resolve) => setTimeout(resolve, 1500));
    expect(api.of('GET', `${APP_PATH}/kill-switch/${RUN}`)).toHaveLength(polls);
  });

  it('shows the refusal on a run page the caller may not read', async () => {
    start(
      `/apps/${APP_ID}/kill-switch/${RUN}`,
      {
        [`GET ${APP_PATH}`]: () => json(200, app('disabled')),
        [`GET ${APP_PATH}/kill-switch/${RUN}`]: () => problem(403, 'FORBIDDEN', 'Only an org admin may read a kill switch run.'),
      },
      signedIn(),
    );
    const alert = await screen.findByRole('alert', {}, { timeout: FIRST_RENDER_MS });
    expect(alert.textContent).toContain('Only an org admin may read a kill switch run.');
    expect(alert.textContent).toContain('req_test_0001');
  });

  it('shows KILL_SWITCH_IN_FLIGHT from enable while a pull still runs', async () => {
    start(
      `/apps/${APP_ID}`,
      page(
        { [`POST ${APP_PATH}/enable`]: () => problem(409, 'KILL_SWITCH_IN_FLIGHT', 'The kill switch is already running for this app.') },
        'disabled',
      ),
      signedIn(),
    );
    const admin = await adminPanel();
    await act(async () => {
      fireEvent.click(await within(admin).findByRole('button', { name: 'Enable' }));
    });
    expect((await within(admin).findByRole('alert')).textContent).toContain('KILL_SWITCH_IN_FLIGHT');
  });

  it('shows KILL_SWITCH_IN_FLIGHT in the dialog', async () => {
    start(
      `/apps/${APP_ID}`,
      page(
        {
          [`POST ${APP_PATH}/kill-switch`]: () =>
            problem(409, 'KILL_SWITCH_IN_FLIGHT', 'The kill switch is already running for this app.', 'Wait for it to finish, then retry.'),
        },
        'disabled',
      ),
      signedIn(),
    );
    const admin = await adminPanel();
    fireEvent.click(await within(admin).findByRole('button', { name: 'Quarantine' }));
    const dialog = await screen.findByRole('dialog', { name: 'Quarantine expenses' });
    await confirmIn(dialog, 'Quarantine');
    const alert = await within(dialog).findByRole('alert');
    expect(alert.textContent).toContain('KILL_SWITCH_IN_FLIGHT');
    expect(alert.textContent).toContain('Wait for it to finish');
  });

  it('enables a quarantined app', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({ [`POST ${APP_PATH}/enable`]: () => json(200, app('active')) }, 'quarantined'),
      signedIn(),
    );
    const admin = await adminPanel();
    expect(within(admin).queryByRole('button', { name: 'Quarantine' })).toBeNull();
    await act(async () => {
      fireEvent.click(await within(admin).findByRole('button', { name: 'Enable' }));
    });
    expect(api.of('POST', `${APP_PATH}/enable`)[0]?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(await toast()).toContain('expenses is active again');
    expect(await within(admin).findByRole('button', { name: 'Disable' })).toBeTruthy();
  });

  it('shows APP_ALREADY_ACTIVE and reads the app again', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page(
        {
          [`GET ${APP_PATH}`]: [() => json(200, app('disabled')), () => json(200, app('active'))],
          [`POST ${APP_PATH}/enable`]: () => problem(409, 'APP_ALREADY_ACTIVE', 'This app is already active.'),
        },
        'disabled',
      ),
      signedIn(),
    );
    const admin = await adminPanel();
    await act(async () => {
      fireEvent.click(await within(admin).findByRole('button', { name: 'Enable' }));
    });
    expect((await within(admin).findByRole('alert')).textContent).toContain('APP_ALREADY_ACTIVE');
    await waitFor(() => expect(api.of('GET', APP_PATH).length).toBeGreaterThan(1));
  });

  it.each([
    ['a member', whoami('member')],
    ['a credential with no role', whoami(null)],
  ])('shows %s no admin control', async (_, me) => {
    start(`/apps/${APP_ID}`, page({ 'GET /v1/whoami': me }), signedIn());
    const admin = await adminPanel();
    expect(await within(admin).findByText(/Only an org admin can disable/)).toBeTruthy();
    expect(within(admin).queryAllByRole('button')).toHaveLength(0);
  });

  it('waits for whoami, and shows its error', async () => {
    start(`/apps/${APP_ID}`, page({ 'GET /v1/whoami': NEVER }), signedIn());
    const admin = await adminPanel();
    expect(within(admin).getByText('Checking your role…')).toBeTruthy();
    expect(within(admin).queryAllByRole('button')).toHaveLength(0);
  });

  it('shows the whoami error instead of the controls', async () => {
    start(`/apps/${APP_ID}`, page({ 'GET /v1/whoami': () => problem(503, 'INTERNAL', 'Down') }), signedIn());
    const admin = await adminPanel();
    expect((await within(admin).findByRole('alert', {}, { timeout: 5000 })).textContent).toContain('Down');
    expect(within(admin).queryAllByRole('button')).toHaveLength(0);
  }, 10_000);

  it('transfers the app to an active person picked from the roster', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        'GET /v1/users': ({ query }) =>
          json(200, {
            users: [
              ...(query.get('q') ? [] : [{ id: OWNER, display_name: 'Olive', email: 'olive@example.com', role: 'admin', status: 'active' }]),
              { id: PERSON, display_name: 'Pat', email: 'pat@example.com', role: 'member', status: 'active' },
            ],
            next_cursor: null,
          }),
        [`PUT ${APP_PATH}/owner`]: () => json(200, app('active', PERSON)),
      }),
      signedIn(),
    );
    const admin = await adminPanel();
    fireEvent.click(await within(admin).findByRole('button', { name: 'Transfer ownership' }));
    const dialog = await screen.findByRole('dialog', { name: 'Transfer expenses' });
    const choices = await within(dialog).findAllByRole('radio');
    expect(choices.map((c) => c.closest('label')?.textContent)).toEqual(['Pat · pat@example.com']);
    expect(api.of('GET', '/v1/users')[0]?.query.get('status')).toBe('active');
    fireEvent.change(within(dialog).getByLabelText('New owner'), { target: { value: 'pa' } });
    await waitFor(() => expect(api.of('GET', '/v1/users').some((c) => c.query.get('q') === 'pa')).toBe(true));
    fireEvent.click(await within(dialog).findByRole('radio', { name: /Pat/ }));
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Transfer' }));
    });
    expect(api.of('PUT', `${APP_PATH}/owner`)[0]?.body).toEqual({ user_id: PERSON });
    expect(await toast()).toBe('expenses now belongs to Pat.');
    expect(screen.getAllByText(PERSON).length).toBeGreaterThan(0);
  });

  it('picks the one person a typed email names', async () => {
    const pat = { id: PERSON, display_name: 'Pat', email: 'pat@example.com', role: 'member', status: 'active' };
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        'GET /v1/users': ({ query }) => json(200, { users: query.get('q') ? [pat] : [], next_cursor: null }),
        [`PUT ${APP_PATH}/owner`]: () => json(200, app('active', PERSON)),
      }),
      signedIn(),
    );
    const admin = await adminPanel();
    fireEvent.click(await within(admin).findByRole('button', { name: 'Transfer ownership' }));
    const dialog = await screen.findByRole('dialog', { name: 'Transfer expenses' });
    expect(await within(dialog).findByText('No other active people.')).toBeTruthy();
    fireEvent.change(within(dialog).getByLabelText('New owner'), { target: { value: 'PAT@example.com' } });
    await waitFor(() => expect((within(dialog).getByRole('radio', { name: /Pat/ }) as HTMLInputElement).checked).toBe(true));
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Transfer' }));
    });
    expect(api.of('PUT', `${APP_PATH}/owner`)[0]?.body).toEqual({ user_id: PERSON });
  });

  it('leaves a shared email to the radios', async () => {
    const pat = (id: string, display_name: string) => ({ id, display_name, email: 'pat@example.com', role: 'member', status: 'active' });
    start(
      `/apps/${APP_ID}`,
      page({
        'GET /v1/users': ({ query }) =>
          json(200, { users: query.get('q') ? [pat(PERSON, 'Pat'), pat('usr_pppppppppppppppppppp', 'Pat Two')] : [], next_cursor: null }),
      }),
      signedIn(),
    );
    const admin = await adminPanel();
    fireEvent.click(await within(admin).findByRole('button', { name: 'Transfer ownership' }));
    const dialog = await screen.findByRole('dialog', { name: 'Transfer expenses' });
    fireEvent.change(within(dialog).getByLabelText('New owner'), { target: { value: 'pat@example.com' } });
    expect(await within(dialog).findByText('Several people share this address. Pick one.')).toBeTruthy();
    expect(within(dialog).getAllByRole('radio').map((r) => (r as HTMLInputElement).checked)).toEqual([false, false]);
    expect((within(dialog).getByRole('button', { name: 'Transfer' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('does not pick an email match while more people may follow', async () => {
    const pat = { id: PERSON, display_name: 'Pat', email: 'pat@example.com', role: 'member', status: 'active' };
    start(
      `/apps/${APP_ID}`,
      page({
        'GET /v1/users': ({ query }) => json(200, query.get('q') ? { users: [pat], next_cursor: PERSON } : { users: [], next_cursor: null }),
      }),
      signedIn(),
    );
    const admin = await adminPanel();
    fireEvent.click(await within(admin).findByRole('button', { name: 'Transfer ownership' }));
    const dialog = await screen.findByRole('dialog', { name: 'Transfer expenses' });
    fireEvent.change(within(dialog).getByLabelText('New owner'), { target: { value: 'pat@example.com' } });
    expect(await within(dialog).findByRole('button', { name: 'Load more people' })).toBeTruthy();
    expect((within(dialog).getByRole('radio', { name: /Pat/ }) as HTMLInputElement).checked).toBe(false);
  });

  it('reads the app again when a pull is refused', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${APP_PATH}`]: [() => json(200, app('active')), () => json(200, app('disabled'))],
        [`POST ${APP_PATH}/kill-switch`]: () => problem(409, 'APP_NOT_ACTIVE', 'This app is not active.'),
      }),
      signedIn(),
    );
    const admin = await adminPanel();
    fireEvent.click(await within(admin).findByRole('button', { name: 'Disable' }));
    const dialog = await screen.findByRole('dialog', { name: 'Disable expenses' });
    await confirmIn(dialog, 'Disable');
    expect((await within(dialog).findByRole('alert')).textContent).toContain('APP_NOT_ACTIVE');
    await waitFor(() => expect(api.of('GET', APP_PATH).length).toBeGreaterThan(1));
    expect(await within(admin).findByRole('button', { name: 'Enable' })).toBeTruthy();
  });

  it('pages through the roster', async () => {
    const person = (n: number) => ({
      id: `usr_${String(n).repeat(20)}`,
      display_name: `Person ${n}`,
      email: `p${n}@example.com`,
      role: 'member',
      status: 'active',
    });
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        'GET /v1/users': ({ query }) =>
          query.get('cursor')
            ? json(200, { users: [person(2)], next_cursor: null })
            : json(200, { users: [person(1)], next_cursor: 'usr_11111111111111111111' }),
      }),
      signedIn(),
    );
    const admin = await adminPanel();
    fireEvent.click(await within(admin).findByRole('button', { name: 'Transfer ownership' }));
    const dialog = await screen.findByRole('dialog', { name: 'Transfer expenses' });
    await within(dialog).findByRole('radio', { name: /Person 1/ });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Load more people' }));
    await within(dialog).findByRole('radio', { name: /Person 2/ });
    expect(api.of('GET', '/v1/users')[1]?.query.get('cursor')).toBe('usr_11111111111111111111');
    expect(within(dialog).queryByRole('button', { name: 'Load more people' })).toBeNull();
  });

  it('takes a typed usr_ id, and shows OWNER_NOT_ACTIVE with the dialog open', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        'GET /v1/users': () => json(200, { users: [], next_cursor: null }),
        [`PUT ${APP_PATH}/owner`]: () => problem(422, 'OWNER_NOT_ACTIVE', 'The owner must be an active member.'),
      }),
      signedIn(),
    );
    const admin = await adminPanel();
    fireEvent.click(await within(admin).findByRole('button', { name: 'Transfer ownership' }));
    const dialog = await screen.findByRole('dialog', { name: 'Transfer expenses' });
    fireEvent.change(within(dialog).getByLabelText('New owner'), { target: { value: PERSON } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Transfer' }));
    });
    expect(api.of('PUT', `${APP_PATH}/owner`)[0]?.body).toEqual({ user_id: PERSON });
    expect((await within(dialog).findByRole('alert')).textContent).toContain('OWNER_NOT_ACTIVE');
  });

  it('warns of an app with no owner and offers to assign one', async () => {
    const ownerless = { ...app('active'), owner_name: 'Olive Owner', owner_state: 'ownerless' };
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${APP_PATH}`]: () => json(200, ownerless),
        'GET /v1/users': () =>
          json(200, {
            users: [{ id: PERSON, display_name: 'Pat', email: 'pat@example.com', role: 'member', status: 'active' }],
            next_cursor: null,
          }),
        [`PUT ${APP_PATH}/owner`]: () => json(200, app('active', PERSON)),
      }),
      signedIn(),
    );
    expect((await screen.findByText('No owner. Assign one.', {}, { timeout: FIRST_RENDER_MS })).closest('.notice')?.textContent).toContain(
      'Olive Owner is no longer active. An org admin assigns a new owner under Settings.',
    );
    const admin = await adminPanel();
    expect(within(admin).queryByRole('button', { name: 'Transfer ownership' })).toBeNull();
    fireEvent.click(within(admin).getByRole('button', { name: 'Assign owner' }));
    const dialog = await screen.findByRole('dialog', { name: 'Assign an owner to expenses' });
    expect(within(dialog).getByText(/Olive Owner is no longer active/)).toBeTruthy();
    fireEvent.click(await within(dialog).findByRole('radio', { name: /Pat/ }));
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Assign' }));
    });
    expect(api.of('PUT', `${APP_PATH}/owner`)[0]?.body).toEqual({ user_id: PERSON });
    await waitFor(() => expect(screen.queryByText('No owner. Assign one.')).toBeNull());
  });
});

function release(n: number, builtFor: string | null) {
  return {
    release_id: `rel_${String(n).repeat(20)}`,
    number: n,
    label: `R${n}`,
    image_digest: 'sha256:aa',
    manifest_digest: 'sha256:bb',
    source_digest: 'sha256:cc',
    source_commit: null,
    built_for_environment_id: builtFor,
    created_at: '2026-09-28T10:00:00Z',
    actor: { kind: 'user', id: OWNER, via_agent: false },
  };
}

function deployment(n: number, current: boolean) {
  return {
    operation_id: `dep_${String(n).repeat(20)}`,
    kind: 'deploy',
    state: current ? 'healthy' : 'superseded',
    release_id: `rel_${String(n).repeat(20)}`,
    release_number: n,
    failure_code: null,
    current,
    actor: { kind: 'user', id: OWNER, via_agent: false },
    started_at: '2026-09-28T10:00:00Z',
    finished_at: '2026-09-28T10:01:00Z',
  };
}

function operation(state: string) {
  return {
    operation_id: OP,
    kind: 'rollback',
    state,
    app_id: APP_ID,
    environment_id: PROD,
    release_id: `rel_${'2'.repeat(20)}`,
    started_at: '2026-09-29T10:00:00Z',
    finished_at: state === 'healthy' ? '2026-09-29T10:00:05Z' : null,
    failure_code: null,
  };
}

async function openRollback() {
  const prod = await screen.findByRole('region', { name: 'Production prod' }, { timeout: FIRST_RENDER_MS });
  fireEvent.click(within(prod).getByRole('button', { name: 'Roll back Production' }));
  return { prod, dialog: await screen.findByRole('dialog', { name: 'Roll back production' }) };
}

describe('rollback', () => {
  const releases = () =>
    json(200, { items: [release(3, PROD), release(2, PROD), release(1, PREVIEW)], next_before: null });
  const deployments = () =>
    json(200, { environment_id: PROD, items: [deployment(3, true), deployment(2, false)] });

  it('rolls production back to an earlier release built for production and follows it', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${APP_PATH}/releases`]: releases,
        [`GET ${APP_PATH}/environments/${PROD}/deployments`]: deployments,
        [`POST ${APP_PATH}/environments/${PROD}/deployments`]: () => json(202, { operation_id: OP, state: 'pending' }),
        [`GET /v1/operations/${OP}`]: [() => json(200, operation('running')), () => json(200, operation('healthy'))],
      }),
      signedIn(),
    );
    const { prod, dialog } = await openRollback();
    const r3 = await within(dialog).findByRole('radio', { name: /R3/ });
    expect((r3 as HTMLInputElement).disabled).toBe(true);
    expect(within(dialog).getByText(/live now/)).toBeTruthy();
    const r1 = within(dialog).getByRole('radio', { name: /R1/ }) as HTMLInputElement;
    expect(r1.disabled).toBe(true);
    expect(within(dialog).getByText(/built for preview, not production/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('radio', { name: /R2/ }));
    const submit = within(dialog).getByRole('button', { name: 'Roll back' }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    await confirmIn(dialog, 'Roll back');
    const post = api.of('POST', `${APP_PATH}/environments/${PROD}/deployments`)[0];
    expect(post?.body).toEqual({ release_id: `rel_${'2'.repeat(20)}`, kind: 'rollback', confirm: false });
    expect(post?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
    const status = await within(prod).findByText(/Rollback of Production to R2/);
    await within(status).findByText('healthy', {}, { timeout: 3000 });
    expect(api.of('GET', APP_PATH).length).toBeGreaterThan(1);
  });

  it('stops polling the operation once its read fails', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${APP_PATH}/releases`]: releases,
        [`GET ${APP_PATH}/environments/${PROD}/deployments`]: deployments,
        [`POST ${APP_PATH}/environments/${PROD}/deployments`]: () => json(202, { operation_id: OP, state: 'pending' }),
        [`GET /v1/operations/${OP}`]: () => problem(500, 'INTERNAL', 'Something broke.'),
      }),
      signedIn(),
    );
    const { prod, dialog } = await openRollback();
    fireEvent.click(await within(dialog).findByRole('radio', { name: /R2/ }));
    await confirmIn(dialog, 'Roll back');
    await within(prod).findByText(/INTERNAL/, {}, { timeout: 8000 });
    const reads = api.of('GET', `/v1/operations/${OP}`).length;
    await new Promise((r) => setTimeout(r, 2500));
    expect(api.of('GET', `/v1/operations/${OP}`).length).toBe(reads);
  }, 15000);

  it('shows a refusal such as RELEASE_ENVIRONMENT_MISMATCH in the dialog', async () => {
    start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${APP_PATH}/releases`]: () => json(200, { items: [release(3, PROD), release(2, null)], next_before: null }),
        [`GET ${APP_PATH}/environments/${PROD}/deployments`]: deployments,
        [`POST ${APP_PATH}/environments/${PROD}/deployments`]: () =>
          problem(409, 'RELEASE_ENVIRONMENT_MISMATCH', 'This release was built for another environment.'),
      }),
      signedIn(),
    );
    const { dialog } = await openRollback();
    fireEvent.click(await within(dialog).findByRole('radio', { name: /R2/ }));
    await confirmIn(dialog, 'Roll back');
    expect((await within(dialog).findByRole('alert')).textContent).toContain('RELEASE_ENVIRONMENT_MISMATCH');
  });

  it('loads older releases until production has one it may run', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${APP_PATH}/releases`]: ({ query }) =>
          query.get('before') === '8'
            ? json(200, { items: [release(7, PROD), release(6, PREVIEW)], next_before: null })
            : json(200, { items: [release(9, PREVIEW), release(8, PREVIEW)], next_before: 8 }),
        [`GET ${APP_PATH}/environments/${PROD}/deployments`]: () =>
          json(200, { environment_id: PROD, items: [deployment(5, true)] }),
        [`POST ${APP_PATH}/environments/${PROD}/deployments`]: () => json(202, { operation_id: OP, state: 'pending' }),
        [`GET /v1/operations/${OP}`]: () => json(200, operation('running')),
      }),
      signedIn(),
    );
    const { dialog } = await openRollback();
    expect(await within(dialog).findByText(/None of these releases can be picked/)).toBeTruthy();
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Load older releases' }));
    });
    const r7 = (await within(dialog).findByRole('radio', { name: /R7/ })) as HTMLInputElement;
    expect(api.of('GET', `${APP_PATH}/releases`).map((c) => c.query.get('before'))).toEqual([null, '8']);
    expect(within(dialog).queryByRole('button', { name: 'Load older releases' })).toBeNull();
    expect(within(dialog).queryByText(/None of these releases/)).toBeNull();
    expect(r7.disabled).toBe(false);
    fireEvent.click(r7);
    await confirmIn(dialog, 'Roll back');
    expect(api.of('POST', `${APP_PATH}/environments/${PROD}/deployments`)[0]?.body).toEqual({
      release_id: `rel_${'7'.repeat(20)}`,
      kind: 'rollback',
      confirm: false,
    });
  });

  it('lists the migrations a rollback goes back past and needs them accepted', async () => {
    const ahead = {
      environment_id: PROD,
      release_id: `rel_${'2'.repeat(20)}`,
      ledgers: [{ ledger: 'prisma', names: ['20261002090000_add_total', '20261003090000_index'] }],
    };
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${APP_PATH}/releases`]: releases,
        [`GET ${APP_PATH}/environments/${PROD}/deployments`]: deployments,
        [`POST ${APP_PATH}/environments/${PROD}/deployments`]: [
          () => problem(409, 'SCHEMA_AHEAD', 'The database has migrations this release does not have.'),
          () => json(202, { operation_id: OP, state: 'pending' }),
        ],
        [`GET ${APP_PATH}/environments/${PROD}/migrations-ahead`]: () => json(200, ahead),
        [`GET /v1/operations/${OP}`]: () => json(200, operation('running')),
      }),
      signedIn(),
    );
    const { dialog } = await openRollback();
    fireEvent.click(await within(dialog).findByRole('radio', { name: /R2/ }));
    await confirmIn(dialog, 'Roll back');
    const warning = await within(dialog).findByRole('alert');
    expect(warning.textContent).toContain('20261002090000_add_total');
    expect(warning.textContent).toContain('20261003090000_index');
    expect(api.of('GET', `${APP_PATH}/environments/${PROD}/migrations-ahead`)[0]?.query.get('release_id')).toBe(
      `rel_${'2'.repeat(20)}`,
    );
    const anyway = within(dialog).getByRole('button', { name: 'Roll back anyway' }) as HTMLButtonElement;
    expect(anyway.disabled).toBe(true);
    fireEvent.click(within(dialog).getByRole('checkbox', { name: /R2 works with these migrations/ }));
    expect(anyway.disabled).toBe(false);
    await act(async () => {
      fireEvent.click(anyway);
    });
    const posts = api.of('POST', `${APP_PATH}/environments/${PROD}/deployments`);
    expect(posts.map((p) => (p.body as { confirm: boolean }).confirm)).toEqual([false, true]);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('shows who may not list releases', async () => {
    start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${APP_PATH}/releases`]: () => problem(403, 'FORBIDDEN', 'Not allowed'),
        [`GET ${APP_PATH}/environments/${PROD}/deployments`]: deployments,
      }),
      signedIn(),
    );
    const { dialog } = await openRollback();
    expect((await within(dialog).findByRole('alert')).textContent).toContain('FORBIDDEN');
  });
});
