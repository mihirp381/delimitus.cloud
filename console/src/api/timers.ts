import { type ApiClient, must } from './client';
import type { components } from './schema';

export type Schedule = components['schemas']['ScheduleOut'];
export type TimerRun = components['schemas']['TimerRunOut'];
export type TimerRunList = components['schemas']['TimerRunList'];

export interface ScheduleTarget {
  readonly appId: string;
  readonly environmentId: string;
  readonly scheduleId: string;
}

/** How many runs one page of a schedule's history holds. */
export const RUNS_PAGE = 20;

/** Why SSC or a person paused a schedule, in words. */
export const PAUSE_TEXT: Readonly<Record<NonNullable<Schedule['pause_reason']>, string>> = {
  manual: 'paused by hand',
  preview: 'preview never runs on time; run it now instead',
  app_disabled: 'the app is disabled',
  app_quarantined: 'the app is quarantined',
  owner_deactivated: "the app's owner was deactivated",
  builder_access_revoked: 'whoever resumed it is no longer a builder',
};

export const RUN_ERROR_TEXT: Readonly<Record<NonNullable<TimerRun['error']>, string>> = {
  overlap: 'the previous run was still going',
  app_inactive: 'the app was not active',
  owner_inactive: "the app's owner was not active",
  builder_access_revoked: 'whoever resumed it is no longer a builder',
  deleted: 'the schedule was deleted',
  dispatch_unavailable: 'the app could not be reached',
  dispatch_error: 'the call to the app failed',
  http_error: 'the app answered with an error',
  timeout: 'the app took too long',
  abandoned: 'the run was abandoned',
  start_failed: 'the run could not start',
};

function path(t: ScheduleTarget) {
  return {
    app_id: t.appId,
    environment_id: t.environmentId,
    schedule_id: t.scheduleId,
  };
}

export async function pauseSchedule(api: ApiClient, target: ScheduleTarget): Promise<Schedule> {
  return must(
    await api.POST(
      '/v1/apps/{app_id}/environments/{environment_id}/schedules/{schedule_id}/pause',
      { params: { path: path(target) } },
    ),
  );
}

/** Arms the schedule again from now, on the caller's authority (SCHEDULE_CANNOT_RESUME if not). */
export async function resumeSchedule(api: ApiClient, target: ScheduleTarget): Promise<Schedule> {
  return must(
    await api.POST(
      '/v1/apps/{app_id}/environments/{environment_id}/schedules/{schedule_id}/resume',
      { params: { path: path(target) } },
    ),
  );
}

/** Runs the schedule once now, paused or not; the run id to poll. */
export async function runNow(api: ApiClient, target: ScheduleTarget): Promise<string> {
  const body = must(
    await api.POST(
      '/v1/apps/{app_id}/environments/{environment_id}/schedules/{schedule_id}/runs',
      { params: { path: path(target) } },
    ),
  );
  return body.run_id;
}

export async function listRuns(
  api: ApiClient,
  target: ScheduleTarget,
  before: string | null,
  signal?: AbortSignal,
): Promise<TimerRunList> {
  return must(
    await api.GET('/v1/apps/{app_id}/environments/{environment_id}/schedules/{schedule_id}/runs', {
      params: {
        path: path(target),
        query: { limit: RUNS_PAGE, ...(before === null ? {} : { before }) },
      },
      signal,
    }),
  );
}

export async function readRun(
  api: ApiClient,
  target: ScheduleTarget,
  runId: string,
  signal?: AbortSignal,
): Promise<TimerRun> {
  return must(
    await api.GET(
      '/v1/apps/{app_id}/environments/{environment_id}/schedules/{schedule_id}/runs/{run_id}',
      { params: { path: { ...path(target), run_id: runId } }, signal },
    ),
  );
}

export function runFinished(run: TimerRun | undefined): boolean {
  return run !== undefined && run.state !== 'queued' && run.state !== 'running';
}
