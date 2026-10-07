import { type ApiClient, must } from './client';
import { ApiProblem } from './problem';
import type { components } from './schema';

export type Grant = components['schemas']['GrantOut'];
export type GrantInput = components['schemas']['GrantIn'];
export type Grants = components['schemas']['GrantsOut'];
export type GrantsPending = components['schemas']['GrantsPending'];

/**
 * The rules after a change, or, when the change waits for approval (a `202`, decision 016), the
 * approval ids and the rules still in force.
 */
export type GrantsUpdate =
  | { readonly state: 'applied'; readonly grants: Grants }
  | { readonly state: 'pending'; readonly grants: Grants; readonly approvalIds: readonly string[] };

export interface GrantsTarget {
  readonly appId: string;
  readonly environmentId: string;
}

/** What the version the caller last saw looked like: the grants and their ETag. */
export interface GrantsSnapshot {
  readonly grants: Grants;
  readonly etag: string;
}

export const MAX_ATTEMPTS = 3;

const PATH = '/v1/apps/{app_id}/environments/{environment_id}/grants';

/** A grant's identity, as the API compares them: role, subject kind and subject. */
export function grantKey(g: Grant | GrantInput): string {
  return `${g.role}:${g.subject_kind}:${g.subject_id ?? ''}`;
}

export function etagOf(grants: Grants): string {
  return `"${grants.grants_version}"`;
}

/** A grant as the API takes it back: no id, and no subject id for the org. */
export function toInput(g: Grant | GrantInput): GrantInput {
  return g.subject_kind === 'org'
    ? { role: g.role, subject_kind: 'org' }
    : { role: g.role, subject_kind: g.subject_kind, subject_id: g.subject_id };
}

function sameSet(a: readonly (Grant | GrantInput)[], b: readonly (Grant | GrantInput)[]): boolean {
  const left = new Set(a.map(grantKey));
  const right = new Set(b.map(grantKey));
  return left.size === right.size && [...left].every((k) => right.has(k));
}

export async function readGrants(api: ApiClient, target: GrantsTarget): Promise<GrantsSnapshot> {
  const result = await api.GET(PATH, {
    params: { path: { app_id: target.appId, environment_id: target.environmentId } },
  });
  const grants = must(result);
  return { grants, etag: result.response.headers.get('ETag') ?? etagOf(grants) };
}

/**
 * Applies `change` to an environment's grants with If-Match. When someone else changed them
 * first (412 PRECONDITION_STALE), it re-reads the grants and their ETag and applies `change`
 * again to what is there now, up to MAX_ATTEMPTS PUTs. Nothing is sent when `change` leaves the
 * set as it is. A `202` means nothing changed yet and the result names the pending approvals.
 */
export async function updateGrants(
  api: ApiClient,
  target: GrantsTarget,
  change: (grants: readonly Grant[]) => readonly (Grant | GrantInput)[],
  seen?: GrantsSnapshot,
): Promise<GrantsUpdate> {
  let current = seen ?? (await readGrants(api, target));
  for (let attempt = 1; ; attempt += 1) {
    const next = change(current.grants.grants);
    if (sameSet(next, current.grants.grants)) return { state: 'applied', grants: current.grants };
    try {
      const body = must(
        await api.PUT(PATH, {
          params: {
            path: { app_id: target.appId, environment_id: target.environmentId },
            header: { 'If-Match': current.etag },
          },
          body: { grants: next.map(toInput) },
        }),
      );
      return 'approval_ids' in body
        ? { state: 'pending', grants: current.grants, approvalIds: body.approval_ids }
        : { state: 'applied', grants: body };
    } catch (error) {
      const stale = error instanceof ApiProblem && error.code === 'PRECONDITION_STALE';
      if (!stale || attempt >= MAX_ATTEMPTS) throw error;
      current = await readGrants(api, target);
    }
  }
}

/** The change that removes one grant, matched by identity rather than by id. */
export function withoutGrant(target: Grant | GrantInput) {
  const key = grantKey(target);
  return (grants: readonly Grant[]) => grants.filter((g) => grantKey(g) !== key);
}

/** Whether two grants name the same subject, whatever their roles. */
export function sameSubject(a: Grant | GrantInput, b: Grant | GrantInput): boolean {
  return a.subject_kind === b.subject_kind && (a.subject_id ?? null) === (b.subject_id ?? null);
}

/**
 * The change that gives `target` its role. The API allows one grant per subject, so any grant
 * the subject already has is replaced.
 */
export function withGrant(target: GrantInput) {
  return (grants: readonly Grant[]): readonly (Grant | GrantInput)[] => [
    ...grants.filter((g) => !sameSubject(g, target)),
    target,
  ];
}

export type Approval = components['schemas']['Approval'];

/**
 * Asks another org admin to approve exactly this grant set (`widen_audience`, decision 016), for
 * a person whose change was refused with APPROVAL_REQUIRED.
 */
export async function askShareApproval(
  api: ApiClient,
  environmentId: string,
  grants: readonly (Grant | GrantInput)[],
): Promise<Approval> {
  return must(
    await api.POST('/v1/approvals', {
      body: {
        environment_id: environmentId,
        kind: 'widen_audience',
        payload: { grants: grants.map(toInput) },
      },
    }),
  );
}
