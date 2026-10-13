# Web Client (`apps/web`)

React single-page application for Homelab Control Center. Presentation only —
it renders state from the API and collects user intent. It holds no
authoritative state and makes no product decisions, because a browser is an
untrusted client.

**Nothing here is a security boundary.** The route guards and permission
helpers decide what the interface *offers*; the control plane decides what
actually happens, and refuses anything else regardless of what was rendered
(ADR-031). A client-side check that disagreed with the server would be a UX
bug, not a hole.

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

Browser end-to-end tests live in `e2e/` and are run by `make e2e` from the
repository root — that script owns the disposable database, the backend and
the preview server, so Playwright starts nothing itself. It forwards its
arguments, so `./scripts/e2e.sh -g 'expired session'` runs one test.

**There is no Prettier configuration, and Prettier is not the formatter.**
Style is whatever ESLint accepts. Running `npx prettier --write` rewrites
quotes and line breaks across a whole file.

## Talking to the API

Every request goes through `apiRequest` in `src/api/client.ts`. One function,
so credentials, CSRF, timeouts and error parsing are decided once rather than
per call site.

```ts
import { apiRequest } from '@/api/client';

const hosts = await apiRequest<Host[]>('/hosts', { signal });
```

| Decision | Why |
| --- | --- |
| `credentials: 'same-origin'` | The client is served same-origin with the API in both development (Vite proxies `/api`) and production. `'include'` would additionally send cookies cross-origin, which nothing needs. |
| `cache: 'no-store'` | A cache hit after sign-out would re-display a previous session's data. |
| `redirect: 'error'` | An API redirect is never expected, and following one could replay credentials elsewhere. |
| CSRF read per request | The token lives in the readable `hcc_csrf` cookie. Reading it each time is the cheapest way never to send a stale one — after sign-in or sign-out the cookie changes. |
| Nothing in web storage | The session is an `HttpOnly` cookie the client cannot read, which is what keeps an XSS bug from becoming a stolen session. Mirroring any of it into storage would undo that. |

### Errors

`apiRequest` throws `ApiError` for every failure, including transport
failures, so a caller has one type to handle. The `kind` is a discriminated
union, not a status code, and the distinction that matters most is between two
403s:

```ts
import { isPermissionDenied, isSessionExpired, isBackendUnreachable } from '@/api/errors';
```

- `isSessionExpired` — **only** a 401. Re-authenticating is the remedy.
- `isPermissionDenied` — 403 `insufficient_permissions`. Authenticated, just
  not allowed; signing in again would not help, so **do not** send the user to
  the login page.
- `isCsrfFailure` — 403 `csrf_validation_failed`. The session may be fine.
- `isBackendUnreachable` — network, timeout or 5xx. Never report this as a
  sign-out.

A 429 is deliberately **not** retryable: auto-retrying a throttled login is an
automatic password-retry loop that spends the user's remaining budget.

### Types

`src/api/types.ts` is hand-written, and `src/api/openapi.test.ts` validates it
against the backend's real OpenAPI schema (`src/api/openapi.snapshot.json`,
regenerated with `make openapi`). Full generation is milestone 1.15; this is
the drift detection, which is the half worth having first — a type that merely
*looks* right is the failure mode generation exists to prevent. See ADR-037.

## Authentication

`src/auth/` owns it. The state is an explicit union, not a set of booleans:

```
checking · setup_required · unauthenticated · authenticated · unavailable
```

Three independent booleans describe sixteen combinations, most of them
nonsense, and the UI then has to guess precedence — which is where the
"protected page flashes before auth resolves" bug lives. A `switch` over one
`status` either handles every case or fails to typecheck.

Two states exist specifically to avoid lying to the user:

- **`checking`** is not `unauthenticated`. A guard that conflated them would
  show the login page on every reload before the session resolved.
- **`unavailable`** is not `unauthenticated`. A dropped network reported as a
  sign-out is false, discards the user's place, and on a flaky connection
  produces a login page for no reason.

```tsx
const { state, signIn, signOut } = useAuth();
const permissions = usePermissions();   // all false when unauthenticated
```

### Route protection

Protected pages sit inside `<RequireAuth>` in `App.tsx`, so **a page is
protected by where it sits** — the same default-safe arrangement the backend
uses for its protected router. `/login` sits inside `<RequireAnonymous>` and
`/setup` inside `<AllowDuringSetup>`.

The post-sign-in destination travels in router location state, not a query
parameter, and is validated by `lib/redirect.ts` before use: absolute URLs,
protocol-relative `//host`, backslash variants, control characters and the
login and setup paths themselves are all refused. An open redirect is a
phishing primitive.

### Query cache

Keys live in `lib/query-keys.ts`. Sign-in **and** sign-out call
`queryClient.clear()`. The sign-in case matters as much as the other: a
different user may sign in on the same browser, and a surviving cache entry
would show them the previous user's data.

Retries are decided by error kind, not by a count — see `lib/query-client.ts`.

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

`src/mocks/overview.ts` holds static fixtures for the **two** cards on the
Overview page that have no subsystem behind them yet: containers and recent
log entries. The rules (engineering rules 8 and 9, ADR-012):

1. Anything rendering from `src/mocks` sits beneath a `<MockDataBanner />`.
   A test enforces this.
2. Fixtures are **static**, never randomly generated — random values animate
   on each render and read as live telemetry.
3. There is **no fake API client**. Nothing pretends to fetch.
4. Every mock-backed card carries a `Sample` badge.

**Most of the interface is no longer fixtures.** Authentication, user
management, the host registry, monitoring configuration and metrics are all
wired to the real API. The Overview's host table and utilisation figures are
real; its container and log cards are not, and sit in a separately banded
region rather than interleaved with real totals — because a real host count
under a "sample data" banner teaches an operator to disbelieve the one number
on the page that is true.

The directory is deleted in milestone 3.17, which is still open for exactly
one reason: Docker discovery is Phase 6 and log collection is Phase 5, so
there is nothing real to replace those two cards with yet.

**The Hosts page deliberately carries no banner.** Every value on it came
from an agent or from an administrator, and the columns with no value show
nothing rather than a zero — so a banner there would be as misleading as its
absence on the Overview page. Where a host genuinely has no measurements, the
host detail page says so instead of rendering a dashboard of zeroes, because
a zeroed gauge is indistinguishable from a measurement.

## Structure

```
src/
├── index.css              design tokens + base layer
├── main.tsx               mount, StrictMode, BrowserRouter
├── App.tsx                providers + route table
├── api/                   the HTTP boundary
│   ├── client.ts          apiRequest: credentials, CSRF, timeouts
│   ├── errors.ts          ApiError and its discriminated kinds
│   ├── types.ts           the contract, drift-checked against the schema
│   ├── auth.ts            login / logout / me / setup-status
│   ├── users.ts           user administration (administrator only)
│   ├── account.ts         own account, own sessions, password change
│   ├── audit.ts           the audit trail (administrator only)
│   ├── hosts.ts           the host registry: reads any role, writes admin
│   ├── meta.ts            /info, for the General settings panel
│   └── openapi.snapshot.json
├── auth/                  authentication state and routing
│   ├── auth-state.ts      the state machine
│   ├── auth-provider.tsx  the two queries it derives from, and the
│   │                      identity refresh a 401 or 403 triggers
│   ├── auth-gate.tsx      RequireAuth / RequireAnonymous
│   ├── use-auth.ts        useAuth, usePermissions
│   └── permissions.ts     role helpers
├── components/
│   ├── ui/                design-system primitives
│   │                      (incl. modal, select, table, pagination)
│   ├── layout/            shell, sidebar, navigation, brand, account menu
│   │   └── full-page-panel  chrome for every screen outside the shell
│   ├── states/            full-page spinner, backend-unavailable,
│   │                      panel-states (loading / empty / error / denied)
│   ├── error-boundary      recovery screen for a render error
│   ├── mock-data-banner   "this is not real data"
│   ├── not-implemented    honest placeholder for unbuilt sections
│   ├── page-header        per-page title block
│   └── phase-notice       persistent phase marker
├── routes/                one module per page, plus login and setup
│   ├── hosts/             the host registry's detail page and dialogs
│   └── settings/          nested: general, users, account, sessions, audit
├── mocks/                 container and log fixtures (deleted in 3.17)
├── test/                  the fake backend and renderApp helpers
└── lib/                   cn(), query client, query keys, datetime,
                           redirect validation
```

Navigation is driven by a single array in `components/layout/navigation.ts`, so
a section cannot appear in the sidebar but be missing from the drawer.

Settings is **nested routes**, not tab state, so each section is a URL:
linkable, reloadable, and navigable with the browser's Back button. It also
keeps the modules small — one Settings component would end up holding every
section's queries, mutations and dialogs because there would be nowhere else
to put them.

## Notes

- **Recharts is lazy-loaded.** It is roughly half the bundle and used by one
  card, so `routes/overview/resource-chart.tsx` sits behind a `Suspense`
  boundary and ships as a separate chunk.
- **TanStack Query backs the authentication state** as of milestone 1.6.
  Mutations never auto-retry — a retried container stop could act on a
  container the user has since restarted, and a retried sign-in spends another
  attempt from the throttle budget.
- **A refusal refreshes the identity.** A 403 or 401 from any request
  invalidates the cached `/auth/me`, so an operator whose role was reduced
  mid-session stops being offered controls the server now refuses, and a
  deactivated user is not left looking at an authenticated shell where nothing
  works. No polling and no socket: both would spend requests continuously to
  catch an event that happens about never.
- **No optimistic updates on security-sensitive actions.** Deactivation, role
  changes and session revocation refetch rather than patching the cache in
  place. An optimistic update that had to be rolled back would show an account
  as deactivated when it is not, which is the single most misleading thing
  these screens could display.
- **Nothing in the interface is an authorization boundary.** The
  administrative sections are hidden from other roles because a control that
  silently vanishes is kinder than one that errors — and a user who types the
  URL gets the panel's own explanation, because the server refuses the data
  regardless (`docs/SECURITY.md`).
- **`strictPort` is on.** A silent port change would break the dev proxy and
  the backend's configured CORS origin.
- **No external requests.** No web fonts, no CDN, no analytics (ADR-009) — a
  self-hosted tool must work air-gapped and must not leak usage to a third
  party.
