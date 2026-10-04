import assert from 'node:assert/strict';
import { createServer } from 'node:http';
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

function reset() {
  Object.assign(cell, {
    region: `projects/${NUMBER}/regions/${REGION}`,
    audiences: [],
    asked: [],
    gatewayAnswers: [],
    storageStatus: null,
    objects: new Map(),
    sent: [],
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
    return answer(200, data, { 'content-type': kind, 'content-disposition': 'attachment' });
  }
  if (req.method === 'GET') {
    if (req.headers['metadata-flavor'] !== 'Google') return answer(403);
    if (url.pathname === REGION_PATH) return answer(200, cell.region);
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
    return cell.objects.delete(name) ? answer(200, '{"deleted":true}') : answer(...refusal('FILE_NOT_FOUND'));
  }
  if (op === 'get' && !cell.objects.has(name)) return answer(...refusal('FILE_NOT_FOUND'));
  return answer(
    200,
    JSON.stringify({
      url: `${cell.base}/storage/${name}`,
      method: op === 'put' ? 'PUT' : 'GET',
      headers: op === 'put' ? { 'content-type': contentType, 'x-check': 'signed' } : {},
      expires_at: '2026-10-03T12:10:00Z',
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
  await rejects(helper().remove('a.txt'), 'UNAVAILABLE');
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
