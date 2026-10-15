import { describe, expect, it } from 'vitest';

import { FAKE_MODULES } from '@/test/fake-backend';
import { CONTRACT_MODULES } from '@/test/module-catalogue.generated';

/**
 * The fake backend's module catalogue.
 *
 * There is almost nothing to assert here, and that is the point. The fixture
 * used to be a hand-written copy of `contracts/monitoring-modules.json` kept
 * in step by a comment, and it fell six modules behind during Operation
 * Horizon without a test failing — the only thing that could have noticed
 * was the copy. It is generated now, by `scripts/contracts.sh`, from the
 * same registry that writes the JSON, and `make check-contract` fails when
 * the committed file is stale.
 *
 * What is left to check is that the fixture is still *the generated thing*,
 * and that the generator produced something. A regenerated file that was
 * empty, or a future edit that quietly reintroduced a literal here, would
 * both restore the original failure mode.
 */
describe('the fake module catalogue', () => {
  it('is the generated one, not a second copy', () => {
    expect(FAKE_MODULES).toBe(CONTRACT_MODULES);
  });

  it('is not empty, and every module has its interval floor', () => {
    expect(FAKE_MODULES.length).toBeGreaterThan(0);
    for (const module of FAKE_MODULES) {
      expect(module.id).toMatch(/^[a-z_]+$/);
      // The floor is what makes the fixture useful rather than decorative:
      // a catalogue claiming 1 s everywhere would not notice an interface
      // offering an interval the server refuses.
      expect(module.min_interval_seconds).toBeGreaterThan(0);
      expect(module.metrics.length).toBeGreaterThan(0);
    }
  });
});
