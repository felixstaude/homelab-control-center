/**
 * Post-login redirect validation.
 *
 * The route guard records where the user was going so login can return them
 * there. That stored value is attacker-influenceable — it comes from the URL
 * the browser was pointed at — so it is validated before being navigated to.
 *
 * ## What this prevents
 *
 * An open redirect: a link such as
 * `https://hcc.example.com/login?next=https://evil.example.com` that lands the
 * user on an attacker's page *after* they authenticate, with the credibility
 * of having come from the real host. It is a phishing primitive, and the fix
 * is to accept only same-site paths.
 *
 * Rejected, each for a concrete reason:
 *
 * - `https://evil.test/x` — absolute URL, another origin entirely.
 * - `//evil.test/x` — protocol-relative; browsers treat this as another
 *   origin, and it is the form most often missed by a check that only looks
 *   for a scheme.
 * - `/\evil.test` and `\\evil.test` — backslashes, which several browsers
 *   normalise to `/`, turning this into the case above.
 * - `javascript:…`, `data:…` — not navigation at all.
 * - `/login` — not an open redirect, but bouncing a freshly signed-in user
 *   back to the login page is a loop.
 *
 * Anything that is not a plain, rooted, same-site path becomes the dashboard.
 * Failing closed costs the user one click; failing open costs them an account.
 */

/** Where to go when no valid destination was recorded. */
export const DEFAULT_REDIRECT = '/';

/** Paths that must never be a post-login destination. */
const EXCLUDED_PREFIXES = ['/login', '/setup'];

/**
 * Validate a candidate path.
 *
 * @returns the path when it is a safe same-site destination, else
 * {@link DEFAULT_REDIRECT}.
 */
export function safeRedirectPath(candidate: unknown): string {
  if (typeof candidate !== 'string') return DEFAULT_REDIRECT;

  const value = candidate.trim();

  // Must be rooted. A relative path would resolve against whatever page is
  // current, which is not something to reason about here.
  if (!value.startsWith('/')) return DEFAULT_REDIRECT;

  // Protocol-relative, or a backslash variant a browser may normalise into
  // one. Checked before anything else because these *look* like paths.
  if (value.startsWith('//') || value.startsWith('/\\') || value.startsWith('/%2f')) {
    return DEFAULT_REDIRECT;
  }
  if (value.includes('\\')) return DEFAULT_REDIRECT;

  // A scheme cannot appear in a rooted path; if one does, this is not a path.
  if (value.includes('://')) return DEFAULT_REDIRECT;

  // Matching control characters is the entire point here: they are what could
  // smuggle a newline or a null past the structural checks above.
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(value)) return DEFAULT_REDIRECT;

  const path = value.split(/[?#]/)[0] ?? '';
  if (EXCLUDED_PREFIXES.some((prefix) => path === prefix || path.startsWith(`${prefix}/`))) {
    return DEFAULT_REDIRECT;
  }

  return value;
}

/** The shape the route guard stores in the router's location state. */
export type RedirectState = { readonly from?: unknown } | null | undefined;

/**
 * Read the recorded destination out of router location state.
 *
 * Location state rather than a query parameter: it keeps the destination out
 * of the address bar, out of browser history as a shareable link, and out of
 * any proxy access log. It is validated regardless — `history.pushState` is
 * callable by any script on the page, so the value is not trusted merely
 * because it did not arrive in the URL.
 */
export function safeRedirectTarget(state: RedirectState): string {
  if (state === null || state === undefined || typeof state !== 'object') {
    return DEFAULT_REDIRECT;
  }
  return safeRedirectPath(state.from);
}
