# Web Client (`apps/web`)

React single-page application for Homelab Control Center. Presentation only —
it renders state from the API and collects user intent. It holds no
authoritative state and makes no product decisions, because a browser is an
untrusted client.

See [`docs/DEVELOPMENT.md`](../../docs/DEVELOPMENT.md) for setup.

## Commands

```bash
npm run dev          # dev server on :5173, proxies /api to :8000
npm run build        # typecheck + production build
npm run typecheck    # tsc, no emit
npm run lint         # eslint with type-aware rules
npm run test         # vitest
npm run check        # typecheck + lint + test
```

## The design system

**This is the most important thing to understand before adding a page.** The
goal is that seven pages look like one product, not seven independently styled
dashboards.

### Token layer

All design values live in `src/index.css` under `@theme` — surfaces, borders,
text levels, semantic status colours, radii, shadows. Colours are authored in
OKLCH because it is perceptually uniform, so the four elevation steps read as
even increments.

**Never hard-code a colour.** Use the semantic classes:

```tsx
// Correct
<div className="bg-surface-raised border-border-subtle text-text-secondary" />

// Wrong — bypasses the token layer
<div className="bg-slate-800 border-slate-700 text-gray-400" />
```

Dark mode is the default and the design target: this is an infrastructure
console, usually on a second monitor in a dim room.

### Primitives

`src/components/ui/` holds the reusable primitives. Variants are declared with
`cva` so the matrix stays declarative:

| Component | Purpose |
| --- | --- |
| `Button` | All interactive affordances. `asChild` renders a router `Link` as a button without nesting an anchor inside a button. |
| `Card` + parts | Primary content container. Composed from parts, not configured by props. |
| `Badge` | Short status and metadata labels. |
| `StatusDot` / `StatusBadge` | The canonical health vocabulary. |
| `MetricTile` | A single headline metric. |
| `Tooltip` | Radix-backed; supplementary detail only. |
| `Separator`, `Skeleton` | Structure and loading states. |

**To add a variant, extend the primitive's `cva` definition** — do not pass
one-off utility classes at the call site. That is how design systems erode.

### Health vocabulary

`HealthStatus` is `'ok' | 'warn' | 'critical' | 'unknown'`, defined once in
`status-indicator.tsx` and used for hosts, containers, devices and alerts
alike.

`unknown` is first-class and deliberate. A monitoring tool must distinguish
"this is healthy" from "we have not heard from this", and must never render
missing data as OK. The Overview page shows an unreported host as **No data**,
not 0%.

### Accessibility

Not optional; `jsx-a11y` runs in CI.

- **Status is never conveyed by colour alone.** `StatusDot` is `aria-hidden`
  on its own — pair it with text, which `StatusBadge` does for you.
- A skip-to-content link is the first focusable element.
- The mobile drawer is a Radix dialog, so focus trapping, escape-to-close and
  `aria-modal` are handled rather than reimplemented.
- One `<h1>` per page, owned by the shell header; pages render `<h2>` via
  `PageHeader`.
- `:focus-visible` gives one consistent focus ring everywhere.
- `prefers-reduced-motion` is respected.

## Mock data

Phase 0 only. `src/mocks/overview.ts` holds static fixtures for the Overview
page. The rules (engineering rules 8 and 9, ADR-012):

1. Anything rendering from `src/mocks` sits beneath a `<MockDataBanner />`.
   A test enforces this.
2. Fixtures are **static**, never randomly generated — random values animate
   on each render and read as live telemetry.
3. There is **no fake API client**. Nothing pretends to fetch.
4. Every mock-backed card carries a `Sample` badge.

The whole directory is deleted in Phase 2, when the Overview page reads the
real API.

## Structure

```
src/
├── index.css              design tokens + base layer
├── main.tsx               mount, StrictMode, BrowserRouter
├── App.tsx                providers + route table
├── components/
│   ├── ui/                design-system primitives
│   ├── layout/            shell, sidebar, navigation, brand
│   ├── mock-data-banner   "this is not real data"
│   ├── not-implemented    honest placeholder for unbuilt sections
│   ├── page-header        per-page title block
│   └── phase-notice       persistent Phase 0 marker
├── routes/                one module per page
├── mocks/                 Phase 0 fixtures (deleted in Phase 2)
└── lib/                   cn(), query client
```

Navigation is driven by a single array in `components/layout/navigation.ts`, so
a section cannot appear in the sidebar but be missing from the drawer.

## Notes

- **Recharts is lazy-loaded.** It is roughly half the bundle and used by one
  card, so `routes/overview/resource-chart.tsx` sits behind a `Suspense`
  boundary and ships as a separate chunk.
- **TanStack Query is configured but unused.** Phase 0 does no fetching
  (engineering rule 8). The provider is installed so Phase 1 can add its first
  real query without restructuring the tree. Mutations never auto-retry — a
  retried container stop could act on a container the user has since restarted.
- **`strictPort` is on.** A silent port change would break the dev proxy and
  the backend's configured CORS origin.
- **No external requests.** No web fonts, no CDN, no analytics (ADR-009) — a
  self-hosted tool must work air-gapped and must not leak usage to a third
  party.
