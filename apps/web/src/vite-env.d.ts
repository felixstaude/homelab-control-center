/// <reference types="vite/client" />

/**
 * Vite's ambient types, which declare the side-effect CSS import in main.tsx
 * and the `import.meta.env` shape.
 */

interface ImportMetaEnv {
  /**
   * Base URL for control plane requests. Empty means same-origin.
   * See `.env.example` — this value is public and embedded in the bundle.
   */
  readonly VITE_API_BASE_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
