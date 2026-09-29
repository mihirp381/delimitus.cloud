import { tanstackRouter } from '@tanstack/router-plugin/vite';
import react from '@vitejs/plugin-react';
import { loadEnv } from 'vite';
import { defineConfig } from 'vitest/config';

// The console calls the API on its own origin: in development and preview this proxy forwards
// /v1 to SSC_API_URL, so the API needs no CORS setting until decisions 018 and 019.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), ['VITE_', 'SSC_']);
  const proxy = { '/v1': { target: env.SSC_API_URL ?? 'http://127.0.0.1:8000', changeOrigin: true } };
  return {
    plugins: [tanstackRouter({ target: 'react', autoCodeSplitting: true }), react()],
    // Dev login is compiled in only for `vite` (import.meta.env.DEV) or with VITE_SSC_DEV_LOGIN=1.
    define: { __SSC_DEV_LOGIN__: JSON.stringify(env.VITE_SSC_DEV_LOGIN === '1') },
    server: { proxy },
    preview: { proxy },
    build: { sourcemap: false },
    test: {
      environment: 'jsdom',
      include: ['test/**/*.test.{ts,tsx}'],
      setupFiles: ['test/setup.ts'],
    },
  };
});
