/**
 * Render the whole application for a test.
 *
 * The real `App` — real providers, real router, real route guards — mounted on
 * a `MemoryRouter` at a chosen path. Combined with the fake backend, a test
 * exercises the same code path a browser does, minus the browser.
 */

import { render, screen, waitFor, type RenderResult } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { App } from '@/App';

/**
 * Mount the app at `path`.
 *
 * `initialEntries` takes the path so deep links and redirect-back behaviour
 * can be driven exactly as they occur in a browser.
 */
export function renderApp(path = '/'): RenderResult {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

/**
 * Wait until authentication has resolved and a real screen is on display.
 *
 * Every entry point renders a "Checking your session…" status first, because
 * the app genuinely does not know yet. Tests wait for that to clear rather
 * than for a fixed delay, so they are not timing-dependent.
 *
 * The second assertion is not redundant. A route guard that redirects renders
 * `<Navigate>`, which produces **no output**, so there is a commit between the
 * spinner and the destination where the document is empty. Waiting only for
 * the spinner to disappear lands in that gap, and the assertions that follow
 * then run against an empty document — which looks exactly like a broken
 * component and is in fact a broken wait.
 */
export async function waitForAuthToResolve(): Promise<void> {
  await waitFor(() => {
    expect(screen.queryByText(/Checking (your session|installation status)…/)).toBeNull();
    expect(document.body.textContent.trim()).not.toBe('');
  });
}
