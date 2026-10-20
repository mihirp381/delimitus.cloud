import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { test } from 'node:test';

import { IdentityRefused, IdentityVerifier, REFUSAL_CODES, tokenFromHeaders, verify } from '../index.js';

const VECTORS = JSON.parse(readFileSync(new URL('../../../../conformance/identity_note/vectors.json', import.meta.url), 'utf8'));
const CASES = Object.fromEntries(VECTORS.cases.map((c) => [c.name, c]));
const opts = (extra = {}) => ({ keys: VECTORS.jwks, now: VECTORS.now, leeway: VECTORS.leeway, ...extra });

function plain(note) {
  const out = { iss: note.iss, aud: note.aud, sub: note.sub, iat: note.iat, exp: note.exp, org: note.org, app: note.app, env: note.env, role: note.role, groups: [...note.groups] };
  if (note.name !== null) out.name = note.name;
  if (note.email !== null) out.email = note.email;
  return out;
}

for (const c of VECTORS.cases) {
  test(`vector: ${c.name}`, async () => {
    if ('ok' in c.expect) {
      assert.deepEqual(plain(await verify(c.token, opts({ audience: c.audience, issuer: c.issuer }))), c.expect.ok);
    } else {
      await assert.rejects(verify(c.token, opts({ audience: c.audience, issuer: c.issuer })), (e) => e instanceof IdentityRefused && e.code === c.expect.refused);
    }
  });
}

test('vectors cover every refusal code', () => {
  const seen = new Set(VECTORS.cases.filter((c) => 'refused' in c.expect).map((c) => c.expect.refused));
  assert.deepEqual([...seen].sort(), [...REFUSAL_CODES].sort());
});

test('wrong issuer when pinned', async () => {
  const c = CASES['user note'];
  await verify(c.token, opts({ audience: c.audience, issuer: VECTORS.issuer }));
  await assert.rejects(verify(c.token, opts({ audience: c.audience, issuer: 'https://keys.delimitus.com/other' })), (e) => e.code === 'wrong_issuer');
});

test("app A's token is refused by app B's verifier", async () => {
  const a = new IdentityVerifier({ audience: VECTORS.audiences.app_a, keys: VECTORS.jwks });
  const b = new IdentityVerifier({ audience: VECTORS.audiences.app_b, keys: VECTORS.jwks });
  const token = CASES['user note'].token;
  assert.match((await a.verify(token, { now: VECTORS.now })).sub, /^usr_/);
  await assert.rejects(b.verify(token, { now: VECTORS.now }), (e) => e.code === 'wrong_audience');
});

test('fromHeaders: any case, Headers object, arrays, and missing', async () => {
  const v = new IdentityVerifier({ audience: VECTORS.audiences.app_a, keys: VECTORS.jwks });
  const token = CASES['user note'].token;
  assert.equal((await v.fromHeaders({ 'x-ssc-identity': token }, { now: VECTORS.now })).email, 'ada@example.com');
  assert.equal(tokenFromHeaders(new Headers({ 'X-SSC-Identity': token })), token);
  assert.equal(tokenFromHeaders({ 'x-ssc-identity': [token] }), token);
  await assert.rejects(v.fromHeaders({ cookie: 'x' }, { now: VECTORS.now }), (e) => e.code === 'missing');
});

test('schedule note has no display fields', async () => {
  const note = await verify(CASES['schedule note'].token, opts({ audience: VECTORS.audiences.app_a }));
  assert.equal(note.isSchedule, true);
  assert.equal(note.role, 'schedule');
  assert.equal(note.name, null);
  assert.equal(note.email, null);
});

test('default clock refuses the fixed vectors as expired', async () => {
  await assert.rejects(verify(CASES['user note'].token, { audience: VECTORS.audiences.app_a, keys: VECTORS.jwks }), (e) => e.code === 'expired');
});

test('JWKS over HTTP, rotation to the second key, unknown kid', async () => {
  const server = createServer((_req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify(VECTORS.jwks));
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  try {
    const url = `http://127.0.0.1:${server.address().port}/jwks.json`;
    const v = new IdentityVerifier({ audience: VECTORS.audiences.app_a, keys: url });
    assert.match((await v.verify(CASES['user note'].token, { now: VECTORS.now })).sub, /^usr_/);
    assert.ok(await v.verify(CASES['user note signed by the second key'].token, { now: VECTORS.now }));
    await assert.rejects(v.verify(CASES['unknown kid'].token, { now: VECTORS.now }), (e) => e.code === 'unknown_key');
  } finally {
    server.close();
  }
});

test('JWKS inline as a data: URL needs no network', async () => {
  const url = `data:application/json;base64,${Buffer.from(JSON.stringify(VECTORS.jwks)).toString('base64')}`;
  const v = new IdentityVerifier({ audience: VECTORS.audiences.app_a, keys: url });
  assert.match((await v.verify(CASES['user note'].token, { now: VECTORS.now })).sub, /^usr_/);
  assert.ok(await v.verify(CASES['user note signed by the second key'].token, { now: VECTORS.now }));
  await assert.rejects(v.verify(CASES['unknown kid'].token, { now: VECTORS.now }), (e) => e.code === 'unknown_key');
});

test('a CommonJS app can require the package by its name', () => {
  const helper = createRequire(import.meta.url)('@delimitus/ssc-identity');
  assert.equal(typeof helper.IdentityVerifier, 'function');
});

test('a key removed from the JWKS is refused once the set is refetched', async (t) => {
  const [keyA, keyB] = VECTORS.jwks.keys;
  let served = { keys: [keyA] };
  const server = createServer((_req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify(served));
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  try {
    t.mock.timers.enable({ apis: ['Date'], now: Date.now() });
    const v = new IdentityVerifier({ audience: VECTORS.audiences.app_a, keys: `http://127.0.0.1:${server.address().port}/jwks.json` });
    const token = CASES['user note'].token;
    assert.match((await v.verify(token, { now: VECTORS.now })).sub, /^usr_/);
    served = { keys: [keyB] };
    t.mock.timers.tick(301_000);
    await assert.rejects(v.verify(token, { now: VECTORS.now }), (e) => e instanceof IdentityRefused && e.code === 'unknown_key');
    assert.ok(await v.verify(CASES['user note signed by the second key'].token, { now: VECTORS.now }));
  } finally {
    server.close();
  }
});

async function jwksServer(answer) {
  let hits = 0;
  const server = createServer((req, res) => {
    hits += 1;
    answer(req, res);
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { url: `http://127.0.0.1:${server.address().port}/jwks.json`, hits: () => hits, close: () => server.close() };
}

test('a JWKS endpoint that is down refuses with unknown_key, not a raw error', async () => {
  const s = await jwksServer(() => {});
  const { url } = s;
  s.close();
  await new Promise((r) => setTimeout(r, 10));
  const v = new IdentityVerifier({ audience: VECTORS.audiences.app_a, keys: url });
  await assert.rejects(v.verify(CASES['user note'].token, { now: VECTORS.now }), (e) => {
    assert.ok(e instanceof IdentityRefused, `${e}`);
    assert.equal(e.code, 'unknown_key');
    assert.ok(!e.message.includes(url));
    return true;
  });
});

test('a JWKS endpoint answering HTML refuses with unknown_key, not a SyntaxError', async () => {
  const s = await jwksServer((_req, res) => {
    res.writeHead(200, { 'content-type': 'text/html' });
    res.end('<html><body>502 Bad Gateway</body></html>');
  });
  try {
    const v = new IdentityVerifier({ audience: VECTORS.audiences.app_a, keys: s.url });
    await assert.rejects(v.verify(CASES['user note'].token, { now: VECTORS.now }), (e) => e instanceof IdentityRefused && e.code === 'unknown_key');
  } finally {
    s.close();
  }
});

test('a JWKS that is not a key set refuses with unknown_key', async () => {
  const s = await jwksServer((_req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{"not":"a jwks"}');
  });
  try {
    const v = new IdentityVerifier({ audience: VECTORS.audiences.app_a, keys: s.url });
    await assert.rejects(v.verify(CASES['user note'].token, { now: VECTORS.now }), (e) => e instanceof IdentityRefused && e.code === 'unknown_key');
  } finally {
    s.close();
  }
});

test('ten concurrent verifies on a cold verifier fetch the JWKS once', async () => {
  const s = await jwksServer((_req, res) => {
    setTimeout(() => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(VECTORS.jwks));
    }, 50);
  });
  try {
    const v = new IdentityVerifier({ audience: VECTORS.audiences.app_a, keys: s.url });
    const notes = await Promise.all(Array.from({ length: 10 }, () => v.verify(CASES['user note'].token, { now: VECTORS.now })));
    assert.equal(notes.length, 10);
    assert.equal(s.hits(), 1);
  } finally {
    s.close();
  }
});
