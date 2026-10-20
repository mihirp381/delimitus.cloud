// Fails `npm run build` when dist/ carries the dev login or a local address (a stray .env).
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

const dir = process.argv[2] ?? 'dist';
const rules = [
  ['ssc-dev-login', /ssc-dev-login/],
  ['a local address', /https?:\/\/(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d|\/|["'])/],
];
const found = [];
for (const entry of readdirSync(dir, { recursive: true, withFileTypes: true })) {
  if (!entry.isFile()) continue;
  const path = join(entry.parentPath, entry.name);
  const text = readFileSync(path, 'utf8');
  for (const [what, re] of rules) if (re.test(text)) found.push(`${path}: ${what}`);
}
if (found.length) {
  console.error(`dist/ is not a production build:\n${found.join('\n')}`);
  process.exit(1);
}
