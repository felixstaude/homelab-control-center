import '@testing-library/jest-dom/vitest';

import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// Unmount React trees between tests so queries never match a previous test's
// DOM, which otherwise produces confusing "found multiple elements" failures.
afterEach(() => {
  cleanup();
});

/**
 * jsdom implements neither `ResizeObserver` nor `matchMedia`, but the DOM lib
 * types declare both as always present. The cast below models them as
 * optional so the guards are honest rather than dead code.
 */
const browserGlobals = globalThis as typeof globalThis & {
  ResizeObserver?: typeof ResizeObserver;
  matchMedia?: typeof matchMedia;
};

// Recharts and Radix measure layout. Stubbing this keeps component tests
// meaningful instead of forcing every chart or popover test into a browser.
browserGlobals.ResizeObserver ??= class {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
};

browserGlobals.matchMedia ??= (query: string) =>
  ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }) as MediaQueryList;
