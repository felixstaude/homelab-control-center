import { fileURLToPath, URL } from 'node:url';

import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
// `defineConfig` is imported from vitest rather than vite: Vite's own
// `UserConfig` type has no `test` key, so the test block below would not
// typecheck against it.
import { defineConfig } from 'vitest/config';

/**
 * Vite configuration.
 *
 * The dev server proxies `/api`, `/healthz` and `/readyz` to the control plane
 * so the browser only ever talks to one origin. That keeps development
 * same-origin — matching production, where the API and the built client are
 * served together — so CORS is not silently relied upon in development and
 * then missing in production.
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],

  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  server: {
    port: 5173,
    // Fail instead of silently moving to another port: a surprise port breaks
    // the backend's configured CORS origin and the proxy assumptions above.
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/healthz': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/readyz': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },

  build: {
    outDir: 'dist',
    sourcemap: true,
    // Surfaces accidental bundle bloat early rather than at deploy time.
    chunkSizeWarningLimit: 700,
  },

  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: true,
  },
});
