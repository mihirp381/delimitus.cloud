// Runs the Playwright smoke test against Postgres and the SSC API in Docker, then removes them.
// Extra arguments go to `playwright test`. Browsers live under node_modules unless
// PLAYWRIGHT_BROWSERS_PATH says otherwise: `PLAYWRIGHT_BROWSERS_PATH=0 npx playwright install chromium`.
import { execFileSync, spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { createServer } from 'node:net';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..');
const project = `ssc-console-e2e-${randomBytes(4).toString('hex')}`;
// An operator token for /internal/v1, which the tests use to add people and groups to the directory.
const MINT_OPERATOR = [
  'import sys; from pathlib import Path; sys.path.insert(0, "tools"); import dev_stack',
  'from ssc_control.api.settings import INTERNAL_AUDIENCE',
  'print(dev_stack.mint(Path("/state"), sub="op_e2e", kind="operator", audience=INTERNAL_AUDIENCE))',
].join('\n');

function compose(...args) {
  return execFileSync('docker', ['compose', '-p', project, '-f', join(here, 'compose.yaml'), ...args], {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'inherit'],
  });
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

let status = 1;
try {
  compose('up', '--build', '--detach', '--wait', '--quiet-pull');
  const published = compose('port', 'api', '8000').trim();
  const apiPort = published.slice(published.lastIndexOf(':') + 1);
  const token = compose('exec', '-T', 'api', 'python', 'tools/dev_stack.py', '--dir', '/state', 'token').trim();
  const operatorToken = compose('exec', '-T', 'api', 'python', '-c', MINT_OPERATOR).trim();
  const env = {
    ...process.env,
    PLAYWRIGHT_BROWSERS_PATH: process.env.PLAYWRIGHT_BROWSERS_PATH ?? '0',
    SSC_API_URL: `http://127.0.0.1:${apiPort}`,
    SSC_CONSOLE_PORT: String(await freePort()),
    SSC_E2E_TOKEN: token,
    SSC_E2E_OPERATOR_TOKEN: operatorToken,
  };
  const playwright = join(root, 'node_modules', '@playwright', 'test', 'cli.js');
  const config = join(here, 'playwright.config.ts');
  const result = spawnSync(process.execPath, [playwright, 'test', '-c', config, ...process.argv.slice(2)], {
    cwd: root,
    env,
    stdio: 'inherit',
  });
  status = result.status ?? 1;
} finally {
  if (status !== 0) {
    try {
      process.stderr.write(compose('logs', '--no-color', '--tail', '200', 'api'));
    } catch {
      // The stack may not have started; the error above says why.
    }
  }
  compose('down', '--volumes', '--remove-orphans');
}
process.exit(status);
