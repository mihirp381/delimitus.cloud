import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { after, before, beforeEach, test } from 'node:test';

import { Data, DataError, IDENTITY_PATH, REGION_PATH, URL_VARIABLE, gatewayUrl } from '../index.js';

const NUMBER = '123456789012';
const REGION = 'us-central1';
const RESULT = {
  columns: [{ name: 'total', type: 'decimal', db_type: 'numeric' }],
  rows: [['12.50'], ['7.00']],
  row_count: 2,
  truncated: true,
  truncated_reason: 'max_rows',
  snapshot_version: 4,
  request_id: 'req-1',
  elapsed_ms: 3,
};

function token(audience) {
  const part = (doc) => Buffer.from(JSON.stringify(doc)).toString('base64url');
  return `${part({ alg: 'RS256' })}.${part({ aud: audience, exp: 4_000_000_000 })}.c2ln`;
}

function refusal(code, status, extra = {}) {
  return [status, JSON.stringify({ error: { code, stage: 'execute', message: 'fixed', fix_owner: 'app', ...extra } })];
}

const cell = {};

function reset() {
  Object.assign(cell, { region: `projects/${NUMBER}/regions/${REGION}`, audiences: [], asked: [], answers: [] });
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const body = Buffer.concat(chunks).toString();
  const answer = (status, data = '') => {
    res.writeHead(status);
    res.end(data);
  };
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
  cell.asked.push([url.pathname, req.headers, JSON.parse(body)]);
  if (cell.answers.length) return answer(...cell.answers.shift());
  return answer(200, JSON.stringify(RESULT));
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

const helper = () => new Data({ url: cell.base, metadata: cell.base });

async function rejects(promise, code) {
  await assert.rejects(promise, (err) => err instanceof DataError && err.code === code);
}

test('the data gateway is found from the metadata server', async () => {
  assert.equal(await gatewayUrl({ metadata: cell.base }), `https://ssc-datagw-${NUMBER}.${REGION}.run.app`);
  cell.region = 'us-central1';
  await assert.rejects(gatewayUrl({ metadata: cell.base }), /region/);
  await rejects(gatewayUrl({ metadata: 'http://127.0.0.1:9' }), 'UNREACHABLE');
});

test('a query carries the statement, the parameters and the workload token', async () => {
  const result = await helper().query('finance', 'select total from t where y = $1', [2026]);
  assert.deepEqual(result, {
    columns: RESULT.columns,
    rows: RESULT.rows,
    rowCount: 2,
    truncated: true,
    truncatedReason: 'max_rows',
    requestId: 'req-1',
  });
  const [[path, headers, body]] = cell.asked;
  assert.equal(path, '/v1/connections/finance/query');
  assert.deepEqual(body, { sql: 'select total from t where y = $1', params: [2026] });
  assert.match(headers.authorization, /^Bearer /);
  assert.equal(headers['x-ssc-identity'], undefined);
  assert.deepEqual(cell.audiences, [cell.base]);
});

test('the asks and the identity note are sent when given', async () => {
  await helper().query('finance', 'select 1', undefined, {
    maxRows: 0,
    maxBytes: 2000,
    timeoutMs: 500,
    identity: 'note.jwt.here',
  });
  const [[, headers, body]] = cell.asked;
  assert.deepEqual(body, { sql: 'select 1', params: [], max_rows: 0, max_bytes: 2000, timeout_ms: 500 });
  assert.equal(headers['x-ssc-identity'], 'note.jwt.here');
});

test('a refusal names its code and is not asked again', async () => {
  cell.answers = [refusal('CONNECTION_NOT_GRANTED', 403)];
  await assert.rejects(
    helper().query('finance', 'select 1'),
    (err) => err.code === 'CONNECTION_NOT_GRANTED' && err.status === 403,
  );
  assert.equal(cell.asked.length, 1);
  cell.answers = [refusal('QUERY_FAILED', 422, { sqlstate: '42P01' })];
  await assert.rejects(
    helper().query('finance', 'select * from nowhere'),
    (err) => err.code === 'QUERY_FAILED' && err.sqlstate === '42P01',
  );
});

test('the data gateway is asked once more while it starts', async () => {
  cell.answers = [[503, 'starting']];
  assert.equal((await helper().query('finance', 'select 1')).rowCount, 2);
  assert.equal(cell.asked.length, 2);
  cell.asked = [];
  cell.answers = [[502, ''], [504, '']];
  await rejects(helper().query('finance', 'select 1'), 'UNAVAILABLE');
  assert.equal(cell.asked.length, 2);
});

test('a gateway that cannot be reached is an error', async () => {
  await rejects(new Data({ url: 'http://127.0.0.1:9', metadata: cell.base }).query('finance', 'select 1'), 'UNREACHABLE');
});

test('the variable names the gateway', async () => {
  process.env[URL_VARIABLE] = `${cell.base}/`;
  const result = await new Data({ metadata: cell.base }).query('finance', 'select 1', [1], { maxRows: 5 });
  assert.equal(result.rowCount, 2);
  assert.equal(cell.asked[0][2].max_rows, 5);
});
