import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { APP_PATH, envPanel, openSection, OWNER, page, PREVIEW, PROD } from './appPage';
import { json, problem } from './fakeApi';
import { signedIn, start } from './harness';

const SCHEDULES = `${APP_PATH}/environments/${PROD}/schedules`;
const SCHEDULE_ID = 'sch_ssssssssssssssssssss';
const ONE = `${SCHEDULES}/${SCHEDULE_ID}`;
const RUN_ID = 'trn_rrrrrrrrrrrrrrrrrrrr';

function run(state: string, extra: Record<string, unknown> = {}) {
  return {
    run_id: RUN_ID,
    schedule_id: SCHEDULE_ID,
    state,
    trigger: 'schedule',
    scheduled_for: '2026-10-06T07:00:00Z',
    created_at: '2026-10-06T07:00:00Z',
    started_at: '2026-10-06T07:00:01Z',
    finished_at: state === 'running' || state === 'queued' ? null : '2026-10-06T07:00:03Z',
    duration_ms: state === 'running' || state === 'queued' ? null : 2500,
    start_ms: 10,
    http_status: state === 'succeeded' ? 200 : null,
    error: null,
    requested_by_user_id: null,
    ...extra,
  };
}

function schedule(state: 'active' | 'paused', extra: Record<string, unknown> = {}) {
  return {
    schedule_id: SCHEDULE_ID,
    environment_id: PROD,
    name: 'nightly-report',
    cron: '0 7 * * 1-5',
    timezone: 'Europe/London',
    method: 'POST',
    path: '/jobs/report',
    timeout_seconds: 60,
    declared_by_user_id: OWNER,
    state,
    pause_reason: state === 'paused' ? 'manual' : null,
    next_run_at: state === 'active' ? '2026-10-07T06:00:00Z' : null,
    last_run: run('succeeded'),
    ...extra,
  };
}

function list(...items: ReturnType<typeof schedule>[]) {
  return { environment_id: PROD, items };
}

async function timers(routes: Parameters<typeof page>[0], status?: Parameters<typeof page>[1]) {
  const result = start(`/apps/app_aaaaaaaaaaaaaaaaaaaa`, page(routes, status), signedIn());
  const panel = await envPanel('Production');
  await openSection(panel, 'Timers');
  await within(panel).findByText('nightly-report');
  return { ...result, panel };
}

describe('timers', () => {
  it('reads nothing until opened, then lists each timer with its next run and last run', async () => {
    const { api, panel } = await timers({ [`GET ${SCHEDULES}`]: () => json(200, list(schedule('active'))) });
    expect(api.of('GET', `${APP_PATH}/environments/${PREVIEW}/schedules`)).toHaveLength(0);
    const table = within(panel).getByRole('table', { name: 'Timers of Production' });
    expect(within(table).getByText('0 7 * * 1-5')).toBeTruthy();
    expect(within(table).getByText('Europe/London')).toBeTruthy();
    expect(within(table).getByText('active')).toBeTruthy();
    expect(within(table).getByText('succeeded')).toBeTruthy();
    expect(table.querySelector('time[datetime="2026-10-07T06:00:00Z"]')).toBeTruthy();
  });

  it('pauses an active timer and resumes a paused one', async () => {
    const { api, panel } = await timers({
      [`GET ${SCHEDULES}`]: [() => json(200, list(schedule('active'))), () => json(200, list(schedule('paused')))],
      [`POST ${ONE}/pause`]: () => json(200, schedule('paused')),
      [`POST ${ONE}/resume`]: () => json(200, schedule('active')),
    });
    await act(async () => {
      fireEvent.click(within(panel).getByRole('button', { name: 'Pause nightly-report in Production' }));
    });
    expect(api.of('POST', `${ONE}/pause`)[0]?.headers.get('Idempotency-Key')).toBeTruthy();
    expect((await within(panel).findByRole('status')).textContent).toContain('Paused nightly-report');
    expect(await within(panel).findByText('paused by hand')).toBeTruthy();
    await act(async () => {
      fireEvent.click(within(panel).getByRole('button', { name: 'Resume nightly-report in Production' }));
    });
    expect(api.of('POST', `${ONE}/resume`)).toHaveLength(1);
    expect((await within(panel).findByRole('status')).textContent).toContain('it now runs on your authority');
  });

  it('shows why a timer cannot be resumed', async () => {
    const { panel } = await timers({
      [`GET ${SCHEDULES}`]: () => json(200, list(schedule('paused', { pause_reason: 'app_disabled' }))),
      [`POST ${ONE}/resume`]: () => problem(409, 'SCHEDULE_CANNOT_RESUME', 'This schedule cannot be resumed yet.'),
    });
    expect(within(panel).getByText('the app is disabled')).toBeTruthy();
    await act(async () => {
      fireEvent.click(within(panel).getByRole('button', { name: 'Resume nightly-report in Production' }));
    });
    expect((await within(panel).findByRole('alert')).textContent).toContain('SCHEDULE_CANNOT_RESUME');
  });

  it('runs a timer now once confirmed, then follows the run to its end', async () => {
    const { api, panel } = await timers({
      [`GET ${SCHEDULES}`]: () => json(200, list(schedule('active'))),
      [`POST ${ONE}/runs`]: () => json(202, { run_id: RUN_ID, state: 'queued' }),
      [`GET ${ONE}/runs/${RUN_ID}`]: [
        () => json(200, run('running', { trigger: 'manual' })),
        () => json(200, run('succeeded', { trigger: 'manual' })),
      ],
    });
    fireEvent.click(within(panel).getByRole('button', { name: 'Run nightly-report now in Production' }));
    const dialog = await screen.findByRole('dialog', { name: 'Run nightly-report now' });
    expect(within(dialog).queryByLabelText(/to confirm/)).toBeNull();
    expect(api.of('POST', `${ONE}/runs`)).toHaveLength(0);
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Run now' }));
    });
    expect(api.of('POST', `${ONE}/runs`)).toHaveLength(1);
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(within(panel).getByRole('status').textContent).toContain('running'));
    await waitFor(() => expect(within(panel).getByRole('status').textContent).toContain('succeeded'), { timeout: 3000 });
    expect(within(panel).getByRole('status').textContent).toContain('HTTP 200');
    await waitFor(() => expect(api.of('GET', SCHEDULES).length).toBeGreaterThan(1));
  });

  it('offers no run now while the app is disabled', async () => {
    const { panel } = await timers({ [`GET ${SCHEDULES}`]: () => json(200, list(schedule('paused'))) }, 'disabled');
    const button = within(panel).getByRole('button', { name: 'Run nightly-report now in Production' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  it('lists the runs, pages back, and shows one run in full', async () => {
    const older = run('failed', { run_id: 'trn_oooooooooooooooooooo', error: 'http_error', http_status: 500, scheduled_for: '2026-10-05T07:00:00Z' });
    const { api, panel } = await timers({
      [`GET ${SCHEDULES}`]: () => json(200, list(schedule('active'))),
      [`GET ${ONE}/runs`]: [() => json(200, { items: [run('succeeded')], next_before: RUN_ID }), () => json(200, { items: [older], next_before: null })],
      [`GET ${ONE}/runs/trn_oooooooooooooooooooo`]: () => json(200, older),
    });
    fireEvent.click(within(panel).getByRole('button', { name: 'Runs of nightly-report in Production' }));
    const dialog = await screen.findByRole('dialog', { name: 'Runs of nightly-report' });
    await within(dialog).findByText('succeeded');
    expect(api.of('GET', `${ONE}/runs`)[0]?.query.get('limit')).toBe('20');
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Load older runs' }));
    });
    expect(api.of('GET', `${ONE}/runs`)[1]?.query.get('before')).toBe(RUN_ID);
    expect(await within(dialog).findByText('the app answered with an error')).toBeTruthy();
    expect(within(dialog).queryByRole('button', { name: 'Load older runs' })).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Details of run trn_oooooooooooooooooooo' }));
    const detail = await within(dialog).findByRole('region', { name: 'Run trn_oooooooooooooooooooo' });
    expect(within(detail).getByText('500')).toBeTruthy();
    expect(within(detail).getByText('the app answered with an error (http_error)')).toBeTruthy();
    expect(within(detail).getByText('2.5 s')).toBeTruthy();
  });
});
