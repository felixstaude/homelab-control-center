import { describe, expect, it } from 'vitest';

import type { PlatformArtifact } from '@/api/types';
import { installCommand, platformLabel } from '@/routes/hosts/install-command';

/**
 * The install command is a *string* with security properties, and these are
 * the properties.
 *
 * It is the one piece of text in this product that tells somebody to run
 * something as root on a machine they own, so what it does and does not
 * contain is worth asserting rather than reviewing.
 */

const AMD64: PlatformArtifact = {
  platform: 'linux/amd64',
  archive: 'hcc-agent_0.2.0_linux_amd64.tar.gz',
  url: 'https://example.test/hcc-agent_0.2.0_linux_amd64.tar.gz',
  sha256: 'c'.repeat(64),
  size_bytes: 2931200,
  tier: 'supported',
};

describe('the install command', () => {
  it('pins the download with the digest the controller supplied', () => {
    const command = installCommand(AMD64, 'https://hcc.example.test');

    // The digest is the whole point: it arrives inside the administrator's
    // authenticated session, so an attacker who controls the download origin
    // cannot make a substituted tarball match it.
    expect(command).toContain(AMD64.sha256);
    expect(command).toContain(`${AMD64.sha256}  ${AMD64.archive}`);
    expect(command).toContain('sha256sum -c -');
  });

  it('is four separate commands, not a pipeline into a shell', () => {
    const lines = installCommand(AMD64, 'https://hcc.example.test').split('\n');

    expect(lines).toHaveLength(4);
    // `sha256sum -c -` exits non-zero, and because these are separate
    // commands a mismatch stops the paste before the `tar`. A pipeline would
    // run the next stage regardless.
    expect(lines[0]).toMatch(/^curl /);
    expect(lines[1]).toMatch(/^echo /);
    expect(lines[2]).toMatch(/^tar /);
    expect(lines[3]).toMatch(/^sudo \.\/hcc-agent install /);

    for (const line of lines) {
      // No `curl … | sudo bash`, no `| sh`, no `$(curl …)`. If one of these
      // ever appears it will be because somebody shortened the command, and
      // the shortening is the thing to catch.
      expect(line).not.toMatch(/\|\s*(sudo\s+)?(ba)?sh\b/);
      expect(line).not.toContain('$(curl');
      expect(line).not.toContain('`curl');
    }
  });

  it('carries no credential and no flag that could take one', () => {
    const command = installCommand(AMD64, 'https://hcc.example.test');

    // The installer prompts with the input hidden. A credential on a command
    // line is readable by every local user through /proc/<pid>/cmdline, and
    // lands in shell history besides.
    for (const forbidden of ['--token ', '--token=', '--credential', '--secret', '--password']) {
      expect(command).not.toContain(forbidden);
    }
  });

  it('points the agent at the origin the administrator is already using', () => {
    // Not a hard-coded hostname and not a guess: the browser's own origin is
    // the one that resolved and whose certificate verified a moment ago.
    const command = installCommand(AMD64, 'https://hcc.example.test:8443');

    expect(command).toContain('--controller https://hcc.example.test:8443');
  });

  it('names the archive for the architecture it was asked about', () => {
    const arm64: PlatformArtifact = {
      ...AMD64,
      platform: 'linux/arm64',
      archive: 'hcc-agent_0.2.0_linux_arm64.tar.gz',
      url: 'https://example.test/hcc-agent_0.2.0_linux_arm64.tar.gz',
      sha256: 'd'.repeat(64),
    };

    const command = installCommand(arm64, 'https://hcc.example.test');

    expect(command).toContain('hcc-agent_0.2.0_linux_arm64.tar.gz');
    expect(command).not.toContain('amd64');
    expect(command).toContain('d'.repeat(64));
  });
});

describe('the architecture label', () => {
  it('names the hardware, not just the Go architecture', () => {
    // `linux/arm64` is what the artifact is named after and the wrong thing
    // to put in front of somebody choosing between a NAS and a Pi.
    expect(platformLabel('linux/amd64')).toContain('x86-64');
    expect(platformLabel('linux/arm64')).toContain('Raspberry Pi');
    expect(platformLabel('linux/arm')).toContain('Raspberry Pi');
  });

  it('passes through an architecture it does not recognise', () => {
    // Rather than rendering "undefined" into a dropdown. A manifest the
    // server accepted but this build has no label for should still be
    // selectable.
    expect(platformLabel('linux/riscv64')).toBe('linux/riscv64');
  });
});
