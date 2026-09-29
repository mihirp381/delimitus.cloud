import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { type Grants, withGrant } from '../src/api/grants';
import { type Call, type Handler, json, problem } from './fakeApi';
import { signedIn, start } from './harness';

const OWNER = 'usr_cccccccccccccccccccc';
const PERSON = 'usr_pppppppppppppppppppp';
const GROUP = 'grp_ffffffffffffffffffff';
const APP_ID = 'app_aaaaaaaaaaaaaaaaaaaa';
const PROD = 'env_prod0000000000000000';
const PREVIEW = 'env_preview0000000000000';
const RUN = 'kil_rrrrrrrrrrrrrrrrrrrr';
const OP = 'dep_oooooooooooooooooooo';

type Status = 'active' | 'disabled' | 'quarantined';

function app(status: Status = 'active', owner = OWNER) {
  return {
    id: APP_ID,
    slug: 'expenses',
    owner_user_id: owner,
    status,
    created_at: '2026-09-28T10:00:00Z',
    environments: [
      { id: PREVIEW, name: 'preview', config_version: 1, grants_version: 0, current_deployment_id: null, url: null },
      { id: PROD, name: 'prod', config_version: 2, grants_version: 3, current_deployment_id: 'dep_eeeeeeeeeeeeeeeeeeee', url: null },
    ],
  };
}

const APP_PATH = `/v1/apps/${APP_ID}`;
const PROD_GRANTS = `${APP_PATH}/environments/${PROD}/grants`;
const PREVIEW_GRANTS = `${APP_PATH}/environments/${PREVIEW}/grants`;
const ORG_USER = { id: 'gnt_000000000000000000o1', role: 'user', subject_kind: 'org', subject_id: null } as const;

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

async function openShare(envName: 'Production' | 'Preview') {
  const name = envName === 'Production' ? 'Production prod' : 'Preview preview';
  const panel = await screen.findByRole('region', { name }, { timeout: FIRST_RENDER_MS });
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
      `/apps/${APP_ID}`,
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
    expect((await within(panel).findByRole('status')).textContent).toBe('Shared production with Finance (user).');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(within(panel).getByText(GROUP)).toBeTruthy();
  });

  it('lets an admin find a person by email and replaces the role they had', async () => {
    const had = { id: 'gnt_000000000000000000u1', role: 'user', subject_kind: 'user', subject_id: PERSON } as const;
    const { api } = start(
      `/apps/${APP_ID}`,
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
      `/apps/${APP_ID}`,
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
    start(`/apps/${APP_ID}`, page({ 'GET /v1/whoami': NEVER }), signedIn());
    const { dialog } = await openShare('Production');
    fireEvent.click(within(dialog).getByRole('radio', { name: 'A person' }));
    expect(within(dialog).getByText('Checking your role…')).toBeTruthy();
    expect(within(dialog).queryByRole('button', { name: 'Find' })).toBeNull();
    expect(within(dialog).getByLabelText('User id')).toBeTruthy();
  });

  it('offers only the builder role on preview', async () => {
    start(`/apps/${APP_ID}`, page(), signedIn());
    const { dialog } = await openShare('Preview');
    const options = within(within(dialog).getByLabelText('Role')).getAllByRole('option');
    expect(options.map((o) => (o as HTMLOptionElement).value)).toEqual(['builder']);
  });

  it('re-reads after a 412 and applies the change to the version it read', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${PROD_GRANTS}`]: [
          () => json(200, grants(PROD, 3)),
          () => json(200, grants(PROD, 4, [ORG_USER]), { ETag: '"4"' }),
        ],
        [`PUT ${PROD_GRANTS}`]: [() => problem(412, 'PRECONDITION_STALE', 'Changed since you read it'), echoGrants(PROD, 5)],
      }),
      signedIn(),
    );
    const { panel, dialog } = await openShare('Production');
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
    expect((await within(panel).findByRole('status')).textContent).toBe(`Shared production with ${GROUP} (user).`);
  });

  it('sends nothing when the subject already has that role', async () => {
    const { api } = start(`/apps/${APP_ID}`, page(), signedIn());
    const { panel, dialog } = await openShare('Production');
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Everyone in the organisation' }));
    await submitShare(dialog);
    expect(api.of('PUT', PROD_GRANTS)).toHaveLength(0);
    expect(await within(panel).findByRole('status')).toBeTruthy();
  });

  it('says a 202 waits for approval and changes nothing', async () => {
    start(
      `/apps/${APP_ID}`,
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
      `/apps/${APP_ID}`,
      page({
        [`PUT ${PROD_GRANTS}`]: () => problem(409, 'APPROVAL_REQUIRED', 'This change needs an approval first.'),
        'POST /v1/approvals': ({ body }) =>
          json(201, { id: 'apr_bbbbbbbbbbbbbbbbbbbb', state: 'pending', ...(body as object) }),
      }),
      signedIn(),
    );
    const { dialog } = await openShare('Production');
    fireEvent.change(within(dialog).getByLabelText('Group name or grp_ id'), { target: { value: GROUP } });
    await submitShare(dialog);
    expect((await within(dialog).findByRole('alert')).textContent).toContain('APPROVAL_REQUIRED');
    expect(within(dialog).getByRole('note').textContent).toContain('Another admin of the organisation must approve');
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Ask for approval' }));
    });
    const ask = api.of('POST', '/v1/approvals')[0];
    expect(ask?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(ask?.body).toEqual({
      environment_id: PROD,
      kind: 'widen_audience',
      payload: {
        grants: [
          { role: 'user', subject_kind: 'org' },
          { role: 'user', subject_kind: 'group', subject_id: GROUP },
        ],
      },
    });
    expect((await within(dialog).findByRole('status')).textContent).toContain('apr_bbbbbbbbbbbbbbbbbbbb');
    expect(within(dialog).queryByRole('note')).toBeNull();
  });

  it('asks for a whole email address before searching people', async () => {
    const { api } = start(`/apps/${APP_ID}`, page(), signedIn());
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
    const owner = { id: 'gnt_000000000000000000b1', role: 'builder', subject_kind: 'user', subject_id: OWNER } as const;
    expect(withGrant(builder)([ORG_USER, owner])).toEqual([owner, builder]);
  });

  it('drops a late search result once a grp_ id was pasted', async () => {
    const other = 'grp_gggggggggggggggggggg';
    const search = held();
    const { api } = start(
      `/apps/${APP_ID}`,
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
    const { api } = start(`/apps/${APP_ID}`, page({ 'GET /v1/groups': search.handler }), signedIn());
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
    const { api } = start(`/apps/${APP_ID}`, page({ 'GET /v1/groups': search.handler }), signedIn());
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
      `/apps/${APP_ID}`,
      page({ [`PUT ${PROD_GRANTS}`]: () => problem(409, 'APPROVAL_REQUIRED', 'This change needs an approval first.') }),
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
      `/apps/${APP_ID}`,
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

function adminPanel() {
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

describe('admin actions', () => {
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
    expect(await screen.findByText('disabled')).toBeTruthy();
    expect(within(admin).getByRole('button', { name: 'Enable' })).toBeTruthy();
    expect(within(admin).getByRole('button', { name: 'Quarantine' })).toBeTruthy();
    expect(within(admin).queryByRole('button', { name: 'Disable' })).toBeNull();
    const polls = api.of('GET', `${APP_PATH}/kill-switch/${RUN}`).length;
    await new Promise((resolve) => setTimeout(resolve, 1500));
    expect(api.of('GET', `${APP_PATH}/kill-switch/${RUN}`)).toHaveLength(polls);
    expect((screen.getByRole('button', { name: 'Roll back Production' }) as HTMLButtonElement).disabled).toBe(true);
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
    expect((await within(admin).findByRole('status')).textContent).toContain('expenses is active again');
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

  it('transfers the app to a person found by email', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        'GET /v1/users': () =>
          json(200, { users: [{ id: PERSON, display_name: 'Pat', email: 'pat@example.com', role: 'member', status: 'active' }] }),
        [`PUT ${APP_PATH}/owner`]: () => json(200, app('active', PERSON)),
      }),
      signedIn(),
    );
    const admin = await adminPanel();
    fireEvent.click(await within(admin).findByRole('button', { name: 'Transfer ownership' }));
    const dialog = await screen.findByRole('dialog', { name: 'Transfer expenses' });
    fireEvent.change(within(dialog).getByLabelText('New owner: email or usr_ id'), { target: { value: 'pat@example.com' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Find' }));
    });
    await within(dialog).findByRole('radio', { name: /Pat/ });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Transfer' }));
    });
    expect(api.of('PUT', `${APP_PATH}/owner`)[0]?.body).toEqual({ user_id: PERSON });
    expect((await within(admin).findByRole('status')).textContent).toBe('expenses now belongs to Pat.');
    expect(screen.getAllByText(PERSON).length).toBeGreaterThan(0);
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

  it('drops a person search that lands after the transfer dialog closed', async () => {
    const search = held();
    const { api } = start(`/apps/${APP_ID}`, page({ 'GET /v1/users': search.handler }), signedIn());
    const admin = await adminPanel();
    fireEvent.click(await within(admin).findByRole('button', { name: 'Transfer ownership' }));
    const dialog = await screen.findByRole('dialog', { name: 'Transfer expenses' });
    fireEvent.change(within(dialog).getByLabelText('New owner: email or usr_ id'), { target: { value: 'pat@example.com' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Find' }));
    await waitFor(() => expect(api.of('GET', '/v1/users')).toHaveLength(1));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await act(async () => {
      search.answer(
        json(200, { users: [{ id: PERSON, display_name: 'Pat', email: 'pat@example.com', role: 'member', status: 'active' }] }),
      );
    });
    fireEvent.click(within(admin).getByRole('button', { name: 'Transfer ownership' }));
    const again = await screen.findByRole('dialog', { name: 'Transfer expenses' });
    expect((within(again).getByRole('button', { name: 'Transfer' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('shows OWNER_NOT_ACTIVE and keeps the dialog open', async () => {
    start(
      `/apps/${APP_ID}`,
      page({ [`PUT ${APP_PATH}/owner`]: () => problem(422, 'OWNER_NOT_ACTIVE', 'The owner must be an active member.') }),
      signedIn(),
    );
    const admin = await adminPanel();
    fireEvent.click(await within(admin).findByRole('button', { name: 'Transfer ownership' }));
    const dialog = await screen.findByRole('dialog', { name: 'Transfer expenses' });
    fireEvent.change(within(dialog).getByLabelText('New owner: email or usr_ id'), { target: { value: PERSON } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Transfer' }));
    });
    expect((await within(dialog).findByRole('alert')).textContent).toContain('OWNER_NOT_ACTIVE');
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
    expect(post?.body).toEqual({ release_id: `rel_${'2'.repeat(20)}`, kind: 'rollback' });
    expect(post?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
    const status = await within(prod).findByText(/Rollback of Production to R2/);
    await within(status).findByText('healthy', {}, { timeout: 3000 });
    expect(api.of('GET', APP_PATH).length).toBeGreaterThan(1);
  });

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
    });
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
