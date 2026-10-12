/// <reference types="vite/client" />

/**
 * Vite's ambient types, which declare the side-effect CSS import in main.tsx
 * and the `import.meta.env` shape.
 */

interface ImportMetaEnv {
  /**
   * Base **path** for control plane requests, e.g. `/api/v1`.
   *
   * A path, never an origin. The client is served same-origin with the API in
   * both development (the Vite dev server proxies `/api`) and production (the
   * control plane serves the built assets) — see `docs/ARCHITECTURE.md` §12.
   * Pointing the client at a different origin would require sending cookies
   * cross-origin, which nothing needs and which widens what a
   * misconfiguration can do, so `src/api/client.ts` refuses a URL here.
   *
   * This value is public: it is embedded verbatim in the bundle.
   */
  readonly VITE_API_BASE_PATH?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
