import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { APP_ID, APP_PATH, envPanel, page, PREVIEW, PROD } from './appPage';
import { type Handler, json, problem } from './fakeApi';
import { signedIn, start } from './harness';

const PREVIEW_DEPLOYMENTS = `${APP_PATH}/environments/${PREVIEW}/deployments`;
const PROD_DEPLOYMENTS = `${APP_PATH}/environments/${PROD}/deployments`;
const SOURCE = 'rel_pppppppppppppppppppp';
const BUILT = 'rel_bbbbbbbbbbbbbbbbbbbb';
const BUILD = 'bld_bbbbbbbbbbbbbbbbbbbb';
const OP = 'dep_oooooooooooooooooooo';

const NO_CHANGES = { blocks: false, changes: [], summarised: false, total: 0 };

function deployment(state: string, current = true) {
  return {
    operation_id: 'dep_pppppppppppppppppppp',
    release_id: SOURCE,
    release_number: 7,
    kind: 'deploy',
    state,
    current,
    started_at: '2026-10-06T09:00:00Z',
    finished_at: '2026-10-06T09:02:00Z',
    failure_code: null,
    actor: { kind: 'user', id: 'usr_cccccccccccccccccccc' },
  };
}

function build(state: string, extra: Record<string, unknown> = {}) {
  const done = state === 'succeeded';
  return {
    build_id: BUILD,
    app_id: APP_ID,
    environment_id: PROD,
    bundle_id: 'bnd_bbbbbbbbbbbbbbbbbbbb',
    state,
    release_id: done ? BUILT : null,
    release_number: done ? 8 : null,
    failure_code: null,
    created_at: '2026-10-06T10:00:00Z',
    started_at: '2026-10-06T10:00:01Z',
    finished_at: done ? '2026-10-06T10:01:00Z' : null,
    capability_diff: NO_CHANGES,
    ...extra,
  };
}

function operation(state: string) {
  return { operation_id: OP, state, failure_code: null };
}

function previewRuns(state = 'healthy'): Record<string, Handler> {
  return {
    [`GET ${PREVIEW_DEPLOYMENTS}`]: () => json(200, { environment_id: PREVIEW, items: [deployment(state)] }),
  };
}

async function openPromote(routes: Record<string, Handler | Handler[]>, status?: Parameters<typeof page>[1]) {
  const result = start(`/apps/${APP_ID}`, page(routes, status), signedIn());
  const panel = await envPanel('Production');
  const button = within(panel).getByRole('button', { name: 'Promote preview to production' }) as HTMLButtonElement;
  return { ...result, panel, button };
}

async function confirmPromote(button: HTMLButtonElement) {
  fireEvent.click(button);
  const dialog = await screen.findByRole('dialog', { name: 'Promote to production' });
  await within(dialog).findByRole('button', { name: 'Promote release 7' });
  expect(dialog.textContent).toContain('Production builds release 7 of preview from the same source.');
  fireEvent.change(within(dialog).getByLabelText(/to confirm/), { target: { value: 'expenses' } });
  await act(async () => {
    fireEvent.click(within(dialog).getByRole('button', { name: 'Promote release 7' }));
  });
  return dialog;
}

describe('promote', () => {
  it('is on production only, and off while the app is not active', async () => {
    const { button } = await openPromote({}, 'quarantined');
    expect(button.disabled).toBe(true);
    const preview = await envPanel('Preview');
    expect(within(preview).queryByRole('button', { name: 'Promote preview to production' })).toBeNull();
  });

  it('names the release preview runs, builds it, then deploys what the build made', async () => {
    const { api, panel, button } = await openPromote({
      ...previewRuns(),
      [`POST ${APP_PATH}/promote`]: () => json(202, { build_id: BUILD, state: 'queued', capability_diff: NO_CHANGES }),
      [`GET /v1/builds/${BUILD}`]: [() => json(200, build('running')), () => json(200, build('succeeded'))],
      [`POST ${PROD_DEPLOYMENTS}`]: () => json(202, { operation_id: OP, state: 'pending' }),
      [`GET /v1/operations/${OP}`]: [() => json(200, operation('running')), () => json(200, operation('healthy'))],
    });
    const dialog = await confirmPromote(button);
    const post = api.of('POST', `${APP_PATH}/promote`)[0];
    expect(post?.body).toEqual({ preview_release_id: SOURCE });
    expect(post?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(dialog.hasAttribute('open')).toBe(false);
    const result = await within(panel).findByRole('status', { name: 'Promote' });
    await waitFor(() => expect(result.textContent).toContain('running'));
    const deploy = await within(panel).findByRole(
      'button',
      { name: 'Deploy release 8 to production' },
      { timeout: 3000 },
    );
    expect(result.textContent).toContain('Release 8 is built for production.');
    expect(api.of('POST', PROD_DEPLOYMENTS)).toHaveLength(0);
    fireEvent.click(deploy);
    const confirm = await screen.findByRole('dialog', { name: 'Deploy release 8 to production' });
    fireEvent.change(within(confirm).getByLabelText(/to confirm/), { target: { value: 'expenses' } });
    await act(async () => {
      fireEvent.click(within(confirm).getByRole('button', { name: 'Deploy' }));
    });
    expect(api.of('POST', PROD_DEPLOYMENTS)[0]?.body).toEqual({ release_id: BUILT, kind: 'deploy', confirm: false });
    await waitFor(() => expect(result.textContent).toContain('healthy'), { timeout: 3000 });
    expect(result.textContent).toContain('Deploy of release 8 to production');
  });

  it('shows what the release asks for that production lacks, and where to approve it', async () => {
    const changes = {
      blocks: false,
      summarised: false,
      total: 1,
      changes: [{ kind: 'egress_host_missing', subject: 'api.openai.com', severity: 'medium', consequence: 'Calls to api.openai.com are refused.', approver: 'an org admin' }],
    };
    const { panel, button } = await openPromote({
      ...previewRuns(),
      [`POST ${APP_PATH}/promote`]: () => json(202, { build_id: BUILD, state: 'queued', capability_diff: changes }),
      [`GET /v1/builds/${BUILD}`]: () => json(200, build('succeeded', { capability_diff: changes })),
    });
    await confirmPromote(button);
    const result = await within(panel).findByRole('status', { name: 'Promote' });
    expect(result.textContent).toContain('Calls to api.openai.com are refused.');
    expect(result.textContent).toContain('needs approval from an org admin');
    expect(within(result).getByRole('link', { name: 'See the approvals' }).getAttribute('href')).toBe('/approvals');
  });

  it('shows a failed build with its code and offers no deploy', async () => {
    const { panel, button } = await openPromote({
      ...previewRuns(),
      [`POST ${APP_PATH}/promote`]: () => json(202, { build_id: BUILD, state: 'queued', capability_diff: NO_CHANGES }),
      [`GET /v1/builds/${BUILD}`]: () => json(200, build('failed', { failure_code: 'BUILD_FAILED' })),
    });
    await confirmPromote(button);
    const result = await within(panel).findByRole('status', { name: 'Promote' });
    await waitFor(() => expect(result.textContent).toContain('BUILD_FAILED'));
    expect(within(panel).queryByRole('button', { name: /Deploy release/ })).toBeNull();
  });

  it('says a required check holds the promote back', async () => {
    const detail = 'The app requires named checks on the commit preview runs to pass before promote.';
    const { api, button } = await openPromote({
      ...previewRuns(),
      [`POST ${APP_PATH}/promote`]: () => problem(409, 'REQUIRED_CHECKS_FAILING', 'A check this app requires has not passed.', detail),
    });
    const dialog = await confirmPromote(button);
    const alert = await within(dialog).findByRole('alert');
    expect(alert.textContent).toContain('REQUIRED_CHECKS_FAILING');
    expect(alert.textContent).toContain(detail);
    expect(api.of('GET', `/v1/builds/${BUILD}`)).toHaveLength(0);
  });

  it('will not promote what preview has not got healthy', async () => {
    const { api, button } = await openPromote(previewRuns('running'));
    fireEvent.click(button);
    const dialog = await screen.findByRole('dialog', { name: 'Promote to production' });
    expect((await within(dialog).findByRole('status')).textContent).toContain('Release 7 in preview is running');
    fireEvent.change(within(dialog).getByLabelText(/to confirm/), { target: { value: 'expenses' } });
    expect((within(dialog).getByRole('button', { name: 'Promote release 7' }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.of('POST', `${APP_PATH}/promote`)).toHaveLength(0);
  });

  it('says when preview runs nothing', async () => {
    const { button } = await openPromote({
      [`GET ${PREVIEW_DEPLOYMENTS}`]: () => json(200, { environment_id: PREVIEW, items: [] }),
    });
    fireEvent.click(button);
    const dialog = await screen.findByRole('dialog', { name: 'Promote to production' });
    expect(await within(dialog).findByText('Preview runs nothing yet, so there is nothing to promote.')).toBeTruthy();
  });
});
