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
/**
 * Where the control plane listens.
 *
 * Configurable so the end-to-end run can point at its own disposable
 * instance on a different port without editing this file. Defaults to the
 * port `make dev-server` uses.
 */
const BACKEND = process.env.HCC_DEV_BACKEND ?? 'http://127.0.0.1:8000';

/**
 * Paths the backend owns. Shared by `server` and `preview` so the built
 * client is served the same way the dev client is — see the note on
 * `preview` below.
 */
const API_PROXY = {
  '/api': { target: BACKEND, changeOrigin: true },
  '/healthz': { target: BACKEND, changeOrigin: true },
  '/readyz': { target: BACKEND, changeOrigin: true },
};

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
    proxy: API_PROXY,
  },

  /**
   * `vite preview` serves the production build, and needs the same proxy.
   *
   * Without it, previewing the built client would hit `/api` on the preview
   * server and get a 404 — so the one mode that exercises the shipped bundle
   * would be the one mode that cannot talk to the backend. The browser
   * end-to-end tests run against this, which is why they test the real build
   * rather than the dev server's transformed source.
   */
  preview: {
    port: 4173,
    strictPort: true,
    proxy: API_PROXY,
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
