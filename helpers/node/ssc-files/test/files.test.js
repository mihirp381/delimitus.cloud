import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { after, before, beforeEach, test } from 'node:test';

import { Files, FilesError, IDENTITY_PATH, REGION_PATH, URL_VARIABLE, gatewayUrl } from '../index.js';

const NUMBER = '123456789012';
const REGION = 'us-central1';

function token(audience) {
  const part = (doc) => Buffer.from(JSON.stringify(doc)).toString('base64url');
  return `${part({ alg: 'RS256' })}.${part({ aud: audience, exp: 4_000_000_000 })}.c2ln`;
}

function refusal(code, status = 404) {
  return [status, JSON.stringify({ error: { code, message: 'fixed', stage: 'files', fix_owner: 'app' } })];
}

const cell = {};
const CUT = -1;

function reset() {
  Object.assign(cell, {
    region: `projects/${NUMBER}/regions/${REGION}`,
    audiences: [],
    asked: [],
    gatewayAnswers: [],
    storageStatus: null,
    objects: new Map(),
    sent: [],
    loseDeleteAnswer: null,
    tokenStatus: null,
    storageCut: false,
    maxBytes: 25 * 1024 * 1024,
  });
}

async function readAll(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return Buffer.concat(chunks);
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  const body = await readAll(req);
  const answer = (status, data = '', headers = {}) => {
    if (status === CUT) {
      // A 200 whose body ends before its content-length.
      res.writeHead(200, { ...headers, 'content-length': Buffer.byteLength(data) + 100 });
      res.write(data);
      return setTimeout(() => res.destroy(), 20);
    }
    res.writeHead(status, headers);
    res.end(data);
  };
  if (url.pathname.startsWith('/storage/')) {
    const name = url.pathname.slice('/storage/'.length);
    if (req.method === 'PUT') {
      cell.sent.push(req.headers);
      if (cell.storageStatus !== null) return answer(cell.storageStatus);
      cell.objects.set(name, [body, req.headers['content-type']]);
      return answer(200);
    }
    if (cell.storageStatus !== null || !cell.objects.has(name)) return answer(cell.storageStatus ?? 404);
    const [data, kind] = cell.objects.get(name);
    return answer(cell.storageCut ? CUT : 200, data, { 'content-type': kind, 'content-disposition': 'attachment' });
  }
  if (req.method === 'GET') {
    if (req.headers['metadata-flavor'] !== 'Google') return answer(403);
    if (url.pathname === REGION_PATH) return answer(200, cell.region);
    if (url.pathname === IDENTITY_PATH && cell.tokenStatus !== null) return answer(cell.tokenStatus);
    if (url.pathname === IDENTITY_PATH) {
      const audience = url.searchParams.get('audience');
      assert.equal(url.searchParams.get('format'), 'full');
      cell.audiences.push(audience);
      return answer(200, token(audience));
    }
    return answer(404);
  }
  const op = url.pathname.slice('/v1/files/'.length);
  cell.asked.push([op, req.headers, body.toString()]);
  if (cell.gatewayAnswers.length) return answer(...cell.gatewayAnswers.shift());
  const { name, content_type: contentType } = JSON.parse(body);
  if (op === 'delete') {
    if (!cell.objects.delete(name)) return answer(...refusal('FILE_NOT_FOUND'));
    if (cell.loseDeleteAnswer !== null) {
      const status = cell.loseDeleteAnswer;
      cell.loseDeleteAnswer = null;
      return answer(status, 'upstream connect error');
    }
    return answer(200, '{"deleted":true}');
  }
  if (op === 'get' && !cell.objects.has(name)) return answer(...refusal('FILE_NOT_FOUND'));
  return answer(
    200,
    JSON.stringify({
      url: `${cell.base}/storage/${name}`,
      method: op === 'put' ? 'PUT' : 'GET',
      headers: op === 'put' ? { 'content-type': contentType, 'x-check': 'signed' } : {},
      expires_at: '2026-10-03T12:10:00Z',
      ...(op === 'put' ? { max_bytes: cell.maxBytes } : {}),
    }),
  );
});

before(async () => {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  cell.base = `http://127.0.0.1:${server.address().port}`;
});
after(() => server.close());
beforeEach(() => {
  reset();
  delete process.env[URL_VARIABLE];
});

const helper = () => new Files({ url: cell.base, metadata: cell.base });

async function rejects(promise, code) {
  await assert.rejects(promise, (err) => err instanceof FilesError && err.code === code);
}

test('the data gateway is found from the metadata server', async () => {
  assert.equal(await gatewayUrl({ metadata: cell.base }), `https://ssc-datagw-${NUMBER}.${REGION}.run.app`);
  cell.region = 'us-central1';
  await assert.rejects(gatewayUrl({ metadata: cell.base }), /region/);
  await rejects(gatewayUrl({ metadata: 'http://127.0.0.1:9' }), 'UNREACHABLE');
});

test('a photo goes up and comes back and is deleted', async () => {
  const photo = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 13, 10, 26, 10, 0, 1, 2, 255]);
  const files = helper();
  await files.put('photos/cat.png', photo, { contentType: 'image/png' });
  const got = await files.get('photos/cat.png');
  await files.remove('photos/cat.png');
  assert.deepEqual(got, photo);
  assert.equal(cell.objects.size, 0);
  assert.deepEqual(cell.asked.map(([op]) => op), ['put', 'get', 'delete']);
  assert.deepEqual(JSON.parse(cell.asked[0][2]), { name: 'photos/cat.png', content_type: 'image/png' });
  for (const [, headers] of cell.asked) assert.match(headers.authorization, /^Bearer /);
  assert.deepEqual(cell.audiences, [cell.base]);
  assert.equal(cell.sent[0]['x-check'], 'signed');
  assert.equal(cell.sent[0]['content-type'], 'image/png');
});

test('a get link is handed out for a browser', async () => {
  cell.objects.set('report.html', [Buffer.from('<p>hi</p>'), 'text/html']);
  const link = await helper().link('get', 'report.html');
  assert.deepEqual([link.method, link.url], ['GET', `${cell.base}/storage/report.html`]);
});

test('the data gateway is asked once more while it starts', async () => {
  cell.gatewayAnswers = [[503, 'starting'], refusal('FILES_UNAVAILABLE', 503)];
  await rejects(helper().put('a.txt', 'x'), 'FILES_UNAVAILABLE');
  assert.equal(cell.asked.length, 2);
  cell.asked = [];
  cell.gatewayAnswers = [[502, '']];
  await helper().put('a.txt', 'x');
  assert.equal(cell.asked.length, 2);
  cell.asked = [];
  cell.gatewayAnswers = [[504, ''], [502, '']];
  await rejects(helper().remove('a.txt'), 'BAD_GATEWAY_RESPONSE');
  assert.equal(cell.asked.length, 2);
});

test('a refusal is not asked again', async () => {
  cell.gatewayAnswers = [refusal('APP_NOT_ACTIVE', 403)];
  await assert.rejects(helper().put('a.txt', 'x'), (err) => err.code === 'APP_NOT_ACTIVE' && err.status === 403);
  assert.equal(cell.asked.length, 1);
  await rejects(helper().get('missing.txt'), 'FILE_NOT_FOUND');
});

test('a gateway that cannot be reached is an error', async () => {
  await rejects(new Files({ url: 'http://127.0.0.1:9', metadata: cell.base }).put('a.txt', 'x'), 'UNREACHABLE');
});

test('storage that refuses the transfer is an error', async () => {
  cell.storageStatus = 400;
  await assert.rejects(helper().put('big.bin', 'x'.repeat(10)), (err) => err.code === 'STORAGE_400' && err.status === 400);
});

test('the variable names the gateway', async () => {
  process.env[URL_VARIABLE] = `${cell.base}/`;
  const files = new Files({ metadata: cell.base });
  await files.put('notes/a.txt', 'hello', { contentType: 'text/plain' });
  assert.equal(Buffer.from(await files.get('notes/a.txt')).toString(), 'hello');
  assert.deepEqual(cell.audiences, [cell.base]);
});

test('a retried remove that finds nothing succeeds', async () => {
  // The data gateway deleted the file but its answer was lost (503); the retry finds nothing.
  cell.objects.set('a.txt', [Buffer.from('x'), 'text/plain']);
  cell.loseDeleteAnswer = 503;
  await helper().remove('a.txt');
  assert.equal(cell.objects.size, 0);
  assert.deepEqual(cell.asked.map(([op]) => op), ['delete', 'delete']);
});

test('a remove of a file that never existed is FILE_NOT_FOUND', async () => {
  await assert.rejects(helper().remove('never.txt'), (err) => err.code === 'FILE_NOT_FOUND' && err.status === 404);
  assert.equal(cell.asked.length, 1);
});

test('a retried get that finds nothing is still FILE_NOT_FOUND', async () => {
  cell.gatewayAnswers = [[503, 'starting']];
  await rejects(helper().get('never.txt'), 'FILE_NOT_FOUND');
  assert.equal(cell.asked.length, 2);
});

test('a metadata server that refuses the token is UNREACHABLE', async () => {
  cell.tokenStatus = 403;
  await assert.rejects(helper().put('a.txt', 'x'), (err) => err instanceof FilesError && err.code === 'UNREACHABLE' && /403/.test(err.message));
  assert.equal(cell.asked.length, 0);
});

test('an answer cut short is asked once more, then UNREACHABLE', async () => {
  cell.gatewayAnswers = [[CUT, '{"url']];
  await helper().put('a.txt', 'x');
  assert.equal(cell.asked.length, 2);
  cell.asked = [];
  cell.gatewayAnswers = [[CUT, '{"url'], [CUT, '{"url']];
  await rejects(helper().put('a.txt', 'x'), 'UNREACHABLE');
  assert.equal(cell.asked.length, 2);
});

test('a 200 that is not JSON is BAD_GATEWAY_RESPONSE', async () => {
  cell.gatewayAnswers = [[200, '<html>hello</html>']];
  await assert.rejects(helper().put('a.txt', 'x'), (err) => err instanceof FilesError && err.code === 'BAD_GATEWAY_RESPONSE' && err.status === 200);
  assert.equal(cell.asked.length, 1);
});

test('a download cut short is UNREACHABLE', async () => {
  cell.objects.set('a.txt', [Buffer.from('hello'), 'text/plain']);
  cell.storageCut = true;
  await rejects(helper().get('a.txt'), 'UNREACHABLE');
});

test('a file over the link\'s limit is refused before it is sent', async () => {
  cell.maxBytes = 4;
  for (const body of ['12345', new Uint8Array(5), new Uint8Array(5).buffer, new Blob(['12345']), 'é123']) {
    await rejects(helper().put('a.txt', body), 'FILE_TOO_LARGE');
  }
  assert.deepEqual([cell.sent.length, cell.objects.size], [0, 0]);
  await helper().put('a.txt', '1234');
  assert.equal(cell.objects.get('a.txt')[0].toString(), '1234');
});

test('a CommonJS app can require the package by its name', () => {
  const helper = createRequire(import.meta.url)('@delimitus/ssc-files');
  assert.equal(typeof helper.put, 'function');
});
