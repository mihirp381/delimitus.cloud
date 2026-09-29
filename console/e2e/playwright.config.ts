import { defineConfig, devices } from '@playwright/test';

// e2e/run.mjs sets these after starting the API in Docker.
const port = Number(process.env.SSC_CONSOLE_PORT ?? '4173');
const baseURL = `http://127.0.0.1:${port}`;
const vite = 'node node_modules/vite/bin/vite.js';

export default defineConfig({
  testDir: '.',
  testMatch: '*.spec.ts',
  outputDir: '../test-results',
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: 'list',
  use: { baseURL, trace: 'retain-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    // A production build with the dev login compiled in, served by `vite preview`, whose proxy
    // forwards /v1 to SSC_API_URL.
    command: `${vite} build --outDir dist-e2e --emptyOutDir --logLevel warn && ${vite} preview --outDir dist-e2e --host 127.0.0.1 --port ${port} --strictPort`,
    cwd: '..',
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
    env: { VITE_SSC_DEV_LOGIN: '1', SSC_API_URL: process.env.SSC_API_URL ?? '' },
  },
});
