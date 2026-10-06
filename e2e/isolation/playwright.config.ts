import { defineConfig, devices } from '@playwright/test';

import { fast, proxy, target } from './support';

export default defineConfig({
  testDir: '.',
  testMatch: '*.spec.ts',
  outputDir: 'test-results',
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: target.nightly ? 2 : 1,
  // The nightly page reads the JSON report; the list stays on the console beside it.
  reporter: process.env.PLAYWRIGHT_JSON_OUTPUT_NAME ? [['list'], ['json']] : 'list',
  use: { trace: 'retain-on-failure', ignoreHTTPSErrors: fast, ...proxy },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
});
