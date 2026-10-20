import { spawnSync } from 'node:child_process';
import { cpSync, mkdtempSync, readdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterAll, describe, expect, it } from 'vitest';
import { DEV_LOGIN_MARKER } from '../src/auth/DevLogin';

const ROOT = join(import.meta.dirname, '..');
const SKIP = new Set(['node_modules', 'dist', 'dist-e2e', 'e2e', 'test-results', 'playwright-report', 'blob-report']);
const COPY = readdirSync(ROOT).filter((name) => !SKIP.has(name) && !name.startsWith('.env'));
const made: string[] = [];

/** `vite build` then `scripts/check-dist.mjs` in a temp copy of the console holding `files`. */
function buildWith(files: Record<string, string>): { status: number | null; stderr: string } {
  const dir = mkdtempSync(join(tmpdir(), 'ssc-console-env-'));
  made.push(dir);
  for (const name of COPY) cpSync(join(ROOT, name), join(dir, name), { recursive: true });
  symlinkSync(join(ROOT, 'node_modules'), join(dir, 'node_modules'), 'dir');
  for (const [name, text] of Object.entries(files)) writeFileSync(join(dir, name), text);
  const env = Object.fromEntries(
    Object.entries(process.env).filter(([k]) => !k.startsWith('VITE_') && !k.startsWith('SSC_') && k !== 'NODE_ENV'),
  );
  const vite = join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js');
  const built = spawnSync(process.execPath, [vite, 'build', '--logLevel', 'error'], { cwd: dir, env, encoding: 'utf8' });
  expect(built.status, built.stderr).toBe(0);
  const check = spawnSync(process.execPath, ['scripts/check-dist.mjs'], { cwd: dir, env, encoding: 'utf8' });
  return { status: check.status, stderr: check.stderr };
}

afterAll(() => {
  for (const dir of made) rmSync(dir, { recursive: true, force: true });
});

describe('the post-build check', () => {
  it('runs at the end of npm run build', () => {
    const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'));
    expect(pkg.scripts.build).toMatch(/&& node scripts\/check-dist\.mjs$/);
  });

  it('passes a production build', { timeout: 120_000 }, () => {
    expect(buildWith({}).status).toBe(0);
  });

  it('fails a build that a planted .env gave the dev login', { timeout: 120_000 }, () => {
    const result = buildWith({ '.env': 'VITE_SSC_DEV_LOGIN=1\n' });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain(DEV_LOGIN_MARKER);
  });

  it('fails a build that a planted .env.production pointed at a local address', { timeout: 120_000 }, () => {
    const result = buildWith({ '.env.production': 'VITE_SSC_AUTH_URL=http://localhost:9000\n' });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('local address');
  });

  it('fails a build whose audience a planted .env.local set to 127.0.0.1', { timeout: 120_000 }, () => {
    const result = buildWith({ '.env.local': 'VITE_SSC_API_AUDIENCE=http://127.0.0.1:8000\n' });
    expect(result.status).toBe(1);
  });
});
