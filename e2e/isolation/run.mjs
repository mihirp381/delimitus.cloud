/**
 * Runs the browser isolation suite (SSC-029) against one target, then stops what it started.
 * Extra arguments go to `playwright test`. Browsers live under node_modules unless
 * PLAYWRIGHT_BROWSERS_PATH says otherwise: `PLAYWRIGHT_BROWSERS_PATH=0 npx playwright install chromium webkit`.
 *
 *   node run.mjs fast   the gateway and real Envoy in Docker (`rig.py fast`), for the pre-merge run
 *   node run.mjs live   a staging cell named by SSC_ISO_* (README), signed in by `night-login.ts`
 *                       (SSC_ISO_AUTH_STATE)
 */
import { spawn, spawnSync } from 'node:child_process';
import { createInterface } from 'node:readline';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const [mode, ...rest] = process.argv.slice(2);
const LIVE_REQUIRED = ['SSC_ISO_CELL1_BASE', 'SSC_ISO_AUTH_URL'];

/** Start `rig.py` with `args` and resolve with the settings it announces on its first line. */
function startRig(args) {
  const rig = spawn('uv', ['run', '--quiet', 'python', join('e2e', 'isolation', 'rig.py'), ...args], {
    cwd: root,
    stdio: ['pipe', 'pipe', 'inherit'],
  });
  const settings = new Promise((resolve, reject) => {
    const lines = createInterface({ input: rig.stdout });
    lines.once('line', (line) => resolve(JSON.parse(line)));
    rig.once('exit', (code) => reject(new Error(`rig.py ${args[0]} stopped before it was ready (exit ${code})`)));
  });
  return { rig, settings };
}

/** Close the rig's standard input, which stops it, and wait for it to clean up. */
function stopRig(rig) {
  if (rig.exitCode !== null) return Promise.resolve();
  const exited = new Promise((resolve) => rig.once('exit', resolve));
  rig.stdin.end();
  return exited;
}

if (mode !== 'fast' && mode !== 'live') {
  process.stderr.write('usage: node run.mjs fast|live [playwright test arguments]\n');
  process.exit(2);
}

let rig = null;
let status = 1;
try {
  const env = { ...process.env, PLAYWRIGHT_BROWSERS_PATH: process.env.PLAYWRIGHT_BROWSERS_PATH ?? '0' };
  if (mode === 'fast') {
    const started = startRig(['fast', ...(process.env.SSC_ISO_LIMIT_SECONDS ? ['--limit', process.env.SSC_ISO_LIMIT_SECONDS] : [])]);
    rig = started.rig;
    Object.assign(env, await started.settings);
  } else {
    const missing = LIVE_REQUIRED.filter((name) => !process.env[name]);
    if (missing.length > 0) throw new Error(`live needs ${missing.join(', ')} (README)`);
    env.SSC_ISO_TARGET = 'live';
  }
  const playwright = join(here, 'node_modules', '@playwright', 'test', 'cli.js');
  const result = spawnSync(process.execPath, [playwright, 'test', '-c', join(here, 'playwright.config.ts'), ...rest], {
    cwd: here,
    env,
    stdio: 'inherit',
  });
  status = result.status ?? 1;
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
} finally {
  if (rig) await stopRig(rig);
}
process.exit(status);
