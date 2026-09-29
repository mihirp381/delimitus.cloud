import { execFileSync } from 'node:child_process';
import { mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterAll, describe, expect, it } from 'vitest';
import { DEV_LOGIN_MARKER } from '../src/auth/DevLogin';

const ROOT = join(import.meta.dirname, '..');
const VITE = join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js');
const made: string[] = [];

/** Runs `vite build` into a fresh folder and returns the text of every file it wrote. */
function build(env: Record<string, string>): string[] {
  const out = mkdtempSync(join(tmpdir(), 'ssc-console-build-'));
  made.push(out);
  const clean = Object.fromEntries(
    Object.entries(process.env).filter(([k]) => !k.startsWith('VITE_') && k !== 'NODE_ENV'),
  );
  execFileSync(process.execPath, [VITE, 'build', '--outDir', out, '--emptyOutDir', '--logLevel', 'error'], {
    cwd: ROOT,
    env: { ...clean, ...env },
    stdio: 'pipe',
  });
  const files = readdirSync(out, { recursive: true, withFileTypes: true }).filter((e) => e.isFile());
  return files.map((f) => readFileSync(join(f.parentPath, f.name), 'utf8'));
}

afterAll(() => {
  for (const dir of made) rmSync(dir, { recursive: true, force: true });
});

describe('dev login in the build output', () => {
  it('is absent from a production build', { timeout: 120_000 }, () => {
    const files = build({});
    expect(files.some((text) => text.includes('Sign in to SSC'))).toBe(true);
    expect(files.filter((text) => text.includes(DEV_LOGIN_MARKER))).toEqual([]);
    expect(files.filter((text) => text.includes('tools/dev_stack.py token'))).toEqual([]);
  });

  it('is present only when VITE_SSC_DEV_LOGIN=1', { timeout: 120_000 }, () => {
    const files = build({ VITE_SSC_DEV_LOGIN: '1' });
    expect(files.some((text) => text.includes(DEV_LOGIN_MARKER))).toBe(true);
  });
});
