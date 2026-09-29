import { describe, expect, it, vi } from 'vitest';
import { createApiClient, must } from '../src/api/client';
import { etagOf, type Grants, grantKey, MAX_ATTEMPTS, updateGrants, withoutGrant } from '../src/api/grants';
import { ApiProblem } from '../src/api/problem';
import { fakeApi, json, ORIGIN, problem } from './fakeApi';

const APP = 'app_aaaaaaaaaaaaaaaaaaaa';
const ENV = 'env_bbbbbbbbbbbbbbbbbbbb';
const OWNER = 'usr_cccccccccccccccccccc';
const GRANTS_PATH = `/v1/apps/${APP}/environments/${ENV}/grants`;
const TARGET = { appId: APP, environmentId: ENV };

function grants(version: number, list: Grants['grants']): Grants {
  return { environment_id: ENV, grants_version: version, grants: list };
}

const ORG_USER = { id: 'gnt_000000000000000000o1', role: 'user', subject_kind: 'org', subject_id: null } as const;
const OWNER_BUILDER = { id: 'gnt_000000000000000000b1', role: 'builder', subject_kind: 'user', subject_id: OWNER } as const;

describe('createApiClient', () => {
  it('sends the Bearer token and an Idempotency-Key on each POST only', async () => {
    const api = fakeApi({
      'GET /v1/apps': () => json(200, { apps: [] }),
      'POST /v1/apps': () => json(201, { id: APP }),
    });
    let n = 0;
    const client = createApiClient({
      baseUrl: ORIGIN,
      getToken: () => 'tok-1',
      fetch: api.fetch,
      newIdempotencyKey: () => `key-${++n}`,
    });
    await client.GET('/v1/apps');
    await client.POST('/v1/apps', { body: { slug: 'a' } });
    await client.POST('/v1/apps', { body: { slug: 'b' } });
    expect(api.calls.map((c) => c.headers.get('Authorization'))).toEqual([
      'Bearer tok-1',
      'Bearer tok-1',
      'Bearer tok-1',
    ]);
    expect(api.calls.map((c) => c.headers.get('Idempotency-Key'))).toEqual([null, 'key-1', 'key-2']);
  });

  it('sends no Authorization header without a token and keeps one the caller set', async () => {
    const api = fakeApi({ 'GET /v1/whoami': () => json(200, {}) });
    const client = createApiClient({ baseUrl: ORIGIN, getToken: () => 'session', fetch: api.fetch });
    await client.GET('/v1/whoami', { headers: { Authorization: 'Bearer pasted' } });
    const anonymous = createApiClient({ baseUrl: ORIGIN, getToken: () => null, fetch: api.fetch });
    await anonymous.GET('/v1/whoami');
    expect(api.calls.map((c) => c.headers.get('Authorization'))).toEqual(['Bearer pasted', null]);
  });

  it('throws every refusal as an ApiProblem carrying the problem fields', async () => {
    const api = fakeApi({
      'GET /v1/apps': () => problem(429, 'RATE_LIMITED', 'Too many requests', 'Slow down.'),
    });
    const client = createApiClient({ baseUrl: ORIGIN, getToken: () => 't', fetch: api.fetch });
    const error = await client.GET('/v1/apps').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiProblem);
    expect(error).toMatchObject({
      status: 429,
      code: 'RATE_LIMITED',
      title: 'Too many requests',
      detail: 'Slow down.',
      requestId: 'req_test_0001',
    });
  });

  it('maps a body that is not a problem to an ApiProblem with no code', async () => {
    const api = fakeApi({
      'GET /v1/apps': () =>
        new Response('<html>bad gateway</html>', {
          status: 502,
          statusText: 'Bad Gateway',
          headers: { 'Content-Type': 'text/html' },
        }),
      'GET /v1/whoami': () => json(500, { unexpected: true }),
    });
    const client = createApiClient({ baseUrl: ORIGIN, getToken: () => 't', fetch: api.fetch });
    const gateway = await client.GET('/v1/apps').catch((e: unknown) => e);
    expect(gateway).toMatchObject({ status: 502, code: null, title: 'Bad Gateway', requestId: null });
    const other = await client.GET('/v1/whoami').catch((e: unknown) => e);
    expect(other).toMatchObject({ status: 500, code: null, title: 'HTTP 500' });
  });

  it('calls onUnauthorized on a 401 and still throws', async () => {
    const api = fakeApi({ 'GET /v1/apps': () => problem(401, 'UNAUTHENTICATED', 'Sign in') });
    const onUnauthorized = vi.fn();
    const client = createApiClient({ baseUrl: ORIGIN, getToken: () => 't', fetch: api.fetch, onUnauthorized });
    await expect(client.GET('/v1/apps')).rejects.toMatchObject({ code: 'UNAUTHENTICATED' });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('returns data on success', async () => {
    const api = fakeApi({ 'GET /v1/apps': () => json(200, { apps: [] }) });
    const client = createApiClient({ baseUrl: ORIGIN, getToken: () => 't', fetch: api.fetch });
    expect(must(await client.GET('/v1/apps'))).toEqual({ apps: [] });
  });
});

describe('updateGrants', () => {
  function client(api: ReturnType<typeof fakeApi>) {
    return createApiClient({ baseUrl: ORIGIN, getToken: () => 't', fetch: api.fetch });
  }

  it('sends If-Match with the version the caller saw', async () => {
    const seen = grants(3, [ORG_USER, OWNER_BUILDER]);
    const api = fakeApi({
      [`PUT ${GRANTS_PATH}`]: ({ body }) => json(200, grants(4, (body as Grants).grants as Grants['grants'])),
    });
    const result = await updateGrants(client(api), TARGET, withoutGrant(ORG_USER), {
      grants: seen,
      etag: etagOf(seen),
    });
    expect(api.of('GET', GRANTS_PATH)).toHaveLength(0);
    const [put] = api.of('PUT', GRANTS_PATH);
    expect(put?.headers.get('If-Match')).toBe('"3"');
    expect(put?.body).toEqual({
      grants: [{ role: 'builder', subject_kind: 'user', subject_id: OWNER }],
    });
    expect(result.grants_version).toBe(4);
  });

  it('re-reads the ETag after a 412 and applies the change to the current grants', async () => {
    const seen = grants(3, [ORG_USER, OWNER_BUILDER]);
    const api = fakeApi({
      [`PUT ${GRANTS_PATH}`]: [
        () => problem(412, 'PRECONDITION_STALE', 'Changed since you read it'),
        () => json(200, grants(5, [])),
      ],
      // Someone else removed the owner's builder grant meanwhile.
      [`GET ${GRANTS_PATH}`]: () => json(200, grants(4, [ORG_USER]), { ETag: '"4"' }),
    });
    const result = await updateGrants(client(api), TARGET, withoutGrant(ORG_USER), {
      grants: seen,
      etag: etagOf(seen),
    });
    const puts = api.of('PUT', GRANTS_PATH);
    expect(puts.map((p) => p.headers.get('If-Match'))).toEqual(['"3"', '"4"']);
    expect(puts[1]?.body).toEqual({ grants: [] });
    expect(result.grants_version).toBe(5);
  });

  it('reads first when the caller has no snapshot, and sends nothing when the set is unchanged', async () => {
    const api = fakeApi({
      [`GET ${GRANTS_PATH}`]: () => json(200, grants(7, [OWNER_BUILDER]), { ETag: '"7"' }),
    });
    const result = await updateGrants(client(api), TARGET, withoutGrant(ORG_USER));
    expect(result.grants_version).toBe(7);
    expect(api.of('PUT', GRANTS_PATH)).toHaveLength(0);
  });

  it(`gives up after ${MAX_ATTEMPTS} stale PUTs`, async () => {
    const api = fakeApi({
      [`PUT ${GRANTS_PATH}`]: () => problem(412, 'PRECONDITION_STALE', 'Changed'),
      [`GET ${GRANTS_PATH}`]: () => json(200, grants(9, [ORG_USER]), { ETag: '"9"' }),
    });
    await expect(updateGrants(client(api), TARGET, withoutGrant(ORG_USER))).rejects.toMatchObject({
      status: 412,
    });
    expect(api.of('PUT', GRANTS_PATH)).toHaveLength(MAX_ATTEMPTS);
  });

  it('does not retry other refusals', async () => {
    const api = fakeApi({
      [`PUT ${GRANTS_PATH}`]: () => problem(403, 'FORBIDDEN', 'Not allowed'),
      [`GET ${GRANTS_PATH}`]: () => json(200, grants(2, [ORG_USER]), { ETag: '"2"' }),
    });
    await expect(updateGrants(client(api), TARGET, withoutGrant(ORG_USER))).rejects.toMatchObject({
      code: 'FORBIDDEN',
    });
    expect(api.of('PUT', GRANTS_PATH)).toHaveLength(1);
  });

  it('falls back to the body version when the response has no ETag', async () => {
    const api = fakeApi({
      [`GET ${GRANTS_PATH}`]: () => json(200, grants(6, [ORG_USER])),
      [`PUT ${GRANTS_PATH}`]: () => json(200, grants(7, [])),
    });
    await updateGrants(client(api), TARGET, withoutGrant(ORG_USER));
    expect(api.of('PUT', GRANTS_PATH)[0]?.headers.get('If-Match')).toBe('"6"');
  });

  it('matches grants by role and subject, not by id', () => {
    expect(grantKey({ ...ORG_USER, id: 'gnt_other000000000000000' })).toBe(grantKey(ORG_USER));
    expect(grantKey({ role: 'user', subject_kind: 'org' })).toBe(grantKey(ORG_USER));
    expect(grantKey(OWNER_BUILDER)).not.toBe(grantKey({ ...OWNER_BUILDER, role: 'user' }));
  });
});
