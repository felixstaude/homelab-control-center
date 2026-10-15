import { describe, expect, it } from 'vitest';

import contract from '../../../../contracts/monitoring-modules.json';
import { FAKE_MODULES } from '@/test/fake-backend';

/**
 * The fake backend, checked against the contract it claims to copy.
 *
 * `FAKE_MODULES` is transcribed from `contracts/monitoring-modules.json`
 * because Vite will not import from outside the project root, and widening
 * its allowed paths for a test fixture is not a trade worth making. A
 * transcription with no check is a copy that goes stale, and this one did:
 * six modules landed in the registry during Operation Horizon — filesystem,
 * diskio, network, sensors, docker, systemd — and the fake kept claiming
 * five for the whole of it.
 *
 * Nothing failed, which is the problem. Every browser test ran against a
 * five-module world, so a page that renders eleven rows was never rendered
 * with eleven, and a test asserting a module's health could seed one the
 * fake had never heard of and watch its assertion quietly match nothing.
 *
 * Imported directly here rather than copied again: the path restriction
 * that forced the copy applies to the *application* bundle, and this file is
 * never part of one — Vitest resolves it in Node, outside the browser build.
 */

type ContractMetric = {
  id: string;
  name: string;
  unit: string;
  kind: string;
  label_keys: string[];
};

type ContractModule = {
  id: string;
  name: string;
  platform: string;
  default_interval_seconds: number;
  min_interval_seconds: number;
  schedule_kinds: string[];
  requires_privileges: boolean;
  allows_duplicate_runs: boolean;
  data_schema_version: number;
  metrics: ContractMetric[];
};

const modules = contract.modules as ContractModule[];

describe('the fake module catalogue', () => {
  it('holds exactly the modules the registry declares, in the same order', () => {
    expect(FAKE_MODULES.map((module) => module.id)).toEqual(
      modules.map((module) => module.id),
    );
  });

  it('does not carry descriptions, so this test must not pretend to check them', () => {
    // The one field `FAKE_MODULES` has and the contract does not. Asserted
    // rather than assumed: the comparison below drops `description` to make
    // the shapes match, and if the contract ever gained one that drop would
    // silently stop checking a real field. This fails first instead.
    for (const module of modules) {
      expect(module).not.toHaveProperty('description');
    }
  });

  it.each(modules.map((module) => [module.id, module] as const))(
    'describes %s the way the contract does',
    (id, expected) => {
      const found = FAKE_MODULES.find((module) => module.id === id);
      expect(found).toBeDefined();
      // Compared whole rather than field by field, minus the description the
      // contract does not carry. The interval floors are the point — a
      // fixture using 1 s everywhere would not notice an interface offering
      // an interval the server refuses — but so is `requires_privileges`,
      // which decides whether a module can be switched on at all, and
      // listing the fields that matter is how the next one to matter gets
      // left out.
      const { description: _description, ...actual } = found!;
      expect(actual).toEqual(expected);
    },
  );
});
