import { defineConfig, devices } from '@playwright/test';

// SSC-065: the page as `python -m ssc_landing` serves it, under its content security policy.
const port = Number(process.env.SSC_LANDING_E2E_PORT ?? '8765');
const baseURL = `http://127.0.0.1:${port}`;
const narrow = { width: 360, height: 780 };
const wide = { width: 1440, height: 900 };

export default defineConfig({
  testDir: '.',
  testMatch: '*.spec.ts',
  outputDir: 'test-results',
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: 'list',
  use: { baseURL, trace: 'retain-on-failure' },
  projects: [
    { name: 'chromium-360', use: { ...devices['Desktop Chrome'], viewport: narrow } },
    { name: 'chromium-1440', use: { ...devices['Desktop Chrome'], viewport: wide } },
    { name: 'webkit-360', use: { ...devices['Desktop Safari'], viewport: narrow } },
    { name: 'webkit-1440', use: { ...devices['Desktop Safari'], viewport: wide } },
  ],
  webServer: {
    command: 'uv run --no-sync python -m ssc_landing',
    cwd: '../..',
    url: baseURL,
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      SSC_LANDING_ENV: 'test',
      SSC_LANDING_PAGE: 'landing/index.html',
      SSC_LANDING_ORIGIN: baseURL,
      SSC_LANDING_TRUSTED_HOPS: '0',
      PORT: String(port),
    },
  },
});
